from argparse import ArgumentParser

import torch
import torch.nn as nn
from pytorch_lightning.core.lightning import LightningModule
from torch.optim import AdamW
from transformers import AutoModel, AutoConfig
from transformers.modeling_utils import SequenceSummary
import torch.nn.functional as F

from model.model_utils import TimeDistributed


class SEXLNet(LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams
        self.save_hyperparameters()
        config = AutoConfig.from_pretrained(self.hparams.model_name)
        self.model = AutoModel.from_pretrained(self.hparams.model_name)
        self.pooler = SequenceSummary(config)

        self.classifier = nn.Linear(config.d_model, self.hparams.num_classes)

        self.concept_store = torch.load(self.hparams.concept_store)

        self.phrase_logits = TimeDistributed(nn.Linear(config.d_model,
                                                        self.hparams.num_classes))

        self.topk =  self.hparams.topk
        self.topk_gil_mlp = TimeDistributed(nn.Linear(config.d_model,
                                                      self.hparams.num_classes))

        self.activation = nn.ReLU()

        self.lamda = self.hparams.lamda
        self.gamma = self.hparams.gamma

        self.dropout = nn.Dropout(config.dropout)
        self.loss = nn.CrossEntropyLoss()

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument("--min_lr", default=0, type=float,
                            help="Minimum learning rate.")
        parser.add_argument("--h_dim", type=int,
                            help="Size of the hidden dimension.", default=768)
        parser.add_argument("--n_heads", type=int,
                            help="Number of attention heads.", default=1)
        parser.add_argument("--kqv_dim", type=int,
                            help="Dimensionality of the each attention head.", default=256)
        parser.add_argument("--num_classes", type=float,
                            help="Number of classes.", default=2)
        parser.add_argument("--lr", default=5e-4, type=float,
                            help="Initial learning rate.")
        parser.add_argument("--weight_decay", default=0.01, type=float,
                            help="Weight decay rate.")
        parser.add_argument("--warmup_prop", default=0., type=float,
                            help="Warmup proportion.")
        parser.add_argument("--topk", default=5, type=int,
                            help="Topk GIL concepts")
        parser.add_argument("--lamda", default=0.01, type=float,
                            help="Lamda Parameter")
        parser.add_argument("--gamma", default=0.01, type=float,
                            help="Gamma parameter")
        parser.add_argument(
            "--model_name", default='xlnet-base-cased',  help="Model to use.")
        return parser

    def configure_optimizers(self):
        return AdamW(self.parameters(), lr=self.hparams.lr, betas=(0.9, 0.99),
                     eps=1e-8)
    
    def forward(self, batch):
        self.concept_store = self.concept_store.to(self.model.device)
        tokens, tokens_mask, padded_ndx_tensor, labels = batch

        # step 1: encode the sentence
        sentence_cls, hidden_state = self.forward_classifier(input_ids=tokens,
                                                             token_type_ids=tokens_mask,
                                                             attention_mask=tokens_mask)

        logits = self.classifier(sentence_cls)

        lil_logits = self.lil(hidden_state=hidden_state,
                              nt_idx_matrix=padded_ndx_tensor)
        lil_logits = torch.mean(lil_logits, dim=1)
        gil_logits = self.gil(pooled_input=sentence_cls)

        logits = logits + self.lamda * lil_logits + self.gamma * gil_logits
        predicted_labels = torch.argmax(logits, -1)
        acc = torch.true_divide(
            (predicted_labels == labels).sum(), labels.shape[0])
        return logits, acc

    def gil(self, pooled_input):
        batch_size = pooled_input.size(0)
        inner_products = torch.mm(pooled_input, self.concept_store.T)
        _, topk_indices = torch.topk(inner_products, k=self.topk)
        topk_concepts = torch.index_select(self.concept_store, 0, topk_indices.view(-1))
        topk_concepts = topk_concepts.view(batch_size, self.topk, -1).contiguous()
        gil_topk_logits = self.topk_gil_mlp(topk_concepts)
        gil_logits = torch.mean(gil_topk_logits, dim=1)
        return gil_logits

    def lil(self, hidden_state, nt_idx_matrix):
        phrase_level_hidden = torch.bmm(nt_idx_matrix, hidden_state)
        phrase_level_activations = self.activation(phrase_level_hidden)
        phrase_level_logits = self.phrase_logits(phrase_level_activations)
        return phrase_level_logits


    def forward_classifier(self, input_ids: torch.Tensor, attention_mask: torch.Tensor, token_type_ids: torch.Tensor = None):
        """Returns the pooled token from BERT
        """
        outputs = self.model(input_ids=input_ids,
                             token_type_ids=token_type_ids,
                             attention_mask=attention_mask,
                             output_hidden_states=True)
        hidden_states = outputs["hidden_states"]
        cls_hidden_state = self.dropout(self.pooler(hidden_states[-1]))
        return cls_hidden_state, hidden_states[-1]

    def training_step(self, batch, batch_idx):
        # Load the data into variables
        logits, acc = self(batch)
        loss = self.loss(logits, batch[-1])
        self.log('train_acc', acc, on_step=True,
                 on_epoch=True, prog_bar=True, sync_dist=True)
        return {"loss": loss}


    def validation_step(self, batch, batch_idx):
        # Load the data into variables
        logits, acc = self(batch)

        loss_f = nn.CrossEntropyLoss()
        loss = loss_f(logits, batch[-1])

        self.log('val_loss', loss, on_step=True,
                 on_epoch=True, prog_bar=True, sync_dist=True)
        self.log('val_acc', acc, on_step=True, on_epoch=True,
                 prog_bar=True, sync_dist=True)
        return {"loss": loss}

    def test_step(self, batch, batch_idx):
        # Load the data into variables
        logits, acc = self(batch)

        loss_f = nn.CrossEntropyLoss()
        loss = loss_f(logits, batch[-1])
        return {"loss": loss}

    def get_progress_bar_dict(self):
        tqdm_dict = super().get_progress_bar_dict()
        tqdm_dict.pop("v_num", None)
        tqdm_dict.pop("val_loss_step", None)
        tqdm_dict.pop("val_acc_step", None)
        return tqdm_dict


if __name__ == "__main__":
    sentences = ['This framework generates embeddings for each input sentence',
                 'Sentences are passed as a list of string.',
                 'The quick brown fox jumps over the lazy dog.']
