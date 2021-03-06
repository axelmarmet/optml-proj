import io
import json
import os

import pandas as pd
import seaborn as sns
import torch
import torch.nn.functional as F
import wandb
from PIL import Image
from matplotlib import pyplot as plt
from pytorch_lightning import LightningModule
from pytorch_lightning.loggers import WandbLogger
from torch.optim import AdamW, Adam
from transformers import AutoModelForSequenceClassification, BertForSequenceClassification, \
    PreTrainedTokenizerBase, AutoTokenizer, get_linear_schedule_with_warmup, get_polynomial_decay_schedule_with_warmup
from transformers.modeling_outputs import SequenceClassifierOutput

from src.constants import HEURISTIC_TO_INTEGER, SampleType
from src.model.focalloss import FocalLoss
from src.utils.util import get_logger

PRETRAINED_MODEL_ID = "bert-base-uncased"

log = get_logger(__name__)


class BertForNLI(LightningModule):
    """
    A PyTorch Lightning module that is a wrapper around
    a HuggingFace BERT for sequence classification model.
    The BERT model has a classification head on top and
    will be used to perform Natural Language Inference (NLI).

    Besides wrapping BERT, this class provides the functionality
    of training on MultiNLI dataset and evaluating on MultiNLI
    and HANS dataset. It also adds verbose logging.

    The module uses a linear warmup of configurable length
    and can be configured to either use a polynomial
    or a linear learning rate decay schedule.
    """

    def __init__(self, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        print("-" * 72)
        print(f"self.hparams={self.hparams}")
        print("-" * 72)

        self.bert: BertForSequenceClassification = AutoModelForSequenceClassification.from_pretrained(
            PRETRAINED_MODEL_ID,
            hidden_dropout_prob=self.hparams["hidden_dropout_prob"],
            attention_probs_dropout_prob=self.hparams["attention_probs_dropout_prob"],
            classifier_dropout=self.hparams["classifier_dropout"],
            num_labels=3,
        )
        print(self.bert.config)

        assert isinstance(self.bert, BertForSequenceClassification)

        # initialized in self.setup()
        self.loss_criterion = FocalLoss(self.hparams.focal_loss_gamma)

    def forward(self, input_ids, attention_mask, token_type_ids, label=None, **kwargs) -> SequenceClassifierOutput:
        output: SequenceClassifierOutput = self.bert.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids
        )
        return output

    def mnli_step(self, batch):
        output = self.forward(**batch)

        onehot_labels = F.one_hot(batch["labels"], num_classes=3).float()
        loss = self.loss_criterion(output.logits, onehot_labels)
        preds = output.logits.argmax(dim=-1)
        true_preds = (preds == batch["labels"]).float()

        results = {
            "mnli_loss": loss.mean(),
            "mnli_datapoint_loss": loss,
            "mnli_datapoint_type": batch["type"],
            "mnli_acc": true_preds.mean(),
            "mnli_true_preds": true_preds,
            "mnli_datapoint_count": len(preds),
        }
        return results

    def hans_step(self, batch):
        output = self.forward(**batch)

        onehot_labels = F.one_hot(batch["labels"], num_classes=3).float()
        loss = self.loss_criterion(output.logits, onehot_labels)
        preds = output.logits.argmax(dim=-1)
        labels = batch["labels"]
        heuristic = batch["heuristic"]

        return {"hans_loss": loss, "preds": preds, "labels": labels, "heuristic": heuristic}

    def training_step(self, batch, batch_idx):
        results = self.mnli_step(batch)

        self.log(f"Train/mnli_loss", results["mnli_loss"], on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log(f"Train/mnli_acc", results["mnli_acc"], on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log(f"Train/mnli_datapoint_count", results["mnli_datapoint_count"], on_step=True, on_epoch=True,
                 prog_bar=True, logger=True, add_dataloader_idx=False, reduce_fx="sum")

        if batch_idx == 0 or batch_idx == -1 and self.global_rank == 0 and self.current_epoch in [0, 1]:
            self._log_batch_for_debugging(f"Train/Batch/batch_{batch_idx}", batch)

        # Loss to be used by the optimizer
        results["loss"] = results["mnli_loss"]
        return results

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        if dataloader_idx == 0:
            results = self.mnli_step(batch)
            self.log(f"Valid/mnli_loss", results["mnli_loss"], on_step=True, on_epoch=True, prog_bar=True, logger=True,
                     add_dataloader_idx=False)
            self.log(f"Valid/mnli_acc", results["mnli_acc"], on_step=True, on_epoch=True, prog_bar=True, logger=True,
                     add_dataloader_idx=False)
            self.log(f"Valid/mnli_datapoint_count", results["mnli_datapoint_count"], on_step=True, on_epoch=True,
                     prog_bar=True, logger=True, add_dataloader_idx=False, reduce_fx="sum")
        else:
            results = self.hans_step(batch)

        if batch_idx == 0 or batch_idx == -1 and self.global_rank == 0 and self.current_epoch in [0, 1]:
            self._log_batch_for_debugging(f"Valid/Batch/batch-{batch_idx}_dataloader-{dataloader_idx}", batch)

        return results

    def _log_batch_for_debugging(self, log_key, batch):
        def jsonify(value):
            if isinstance(value, torch.Tensor):
                return value.tolist()
            return value

        debug_tokenizer: PreTrainedTokenizerBase = AutoTokenizer.from_pretrained(PRETRAINED_MODEL_ID)
        batch = dict(batch)  # do not modify the original batch dict
        batch["txt"] = debug_tokenizer.batch_decode(batch["input_ids"])

        batch_json = json.dumps({k: jsonify(v) for k, v in batch.items()})
        log.info(f"{log_key}:\n{batch_json}")

        batch_df = pd.DataFrame({k: [str(jsonify(e)) for e in v] for k, v in batch.items()})
        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger: WandbLogger = logger
                logger.log_text(f"{log_key}", dataframe=batch_df)

    @staticmethod
    def log_plot_to_wandb(wandb_logger, log_key, backend=plt, dpi=200):
        with io.BytesIO() as f:
            backend.savefig(f, dpi=dpi, format='png')
            im = Image.open(f)
            wandb_logger.log({log_key: wandb.Image(im)})

    def _log_loss_histogram(self, losses, split, metric_name, log_df=False, bins=72, height=4.5, dpi=200):
        df = pd.DataFrame({metric_name: losses})
        grid = sns.displot(data=df, x=metric_name, height=height, bins=bins)
        grid.fig.suptitle(f"[Epoch {self.current_epoch}] {split} {metric_name} histogram", fontsize=16)
        grid.set_titles(col_template="{col_name}", row_template="{row_name}")
        grid.tight_layout()

        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger.experiment.log({f'{split}/Verbose/{metric_name}_histogram': wandb.Histogram(losses)})
                self.log_plot_to_wandb(logger.experiment, f'{split}/Verbose/{metric_name}_histogram_seaborn',
                                       backend=grid, dpi=dpi)
                if log_df:
                    logger.experiment.log({f"{split}/Verbose/{metric_name}_df": df})

        plt.close()

    def _log_mnli_metrics_per_sample_type(self, prefix: str, types, losses, true_preds):
        for sample_type in SampleType:
            mask = types == sample_type.value
            loss_per_type = losses[mask].mean()
            acc_per_type = true_preds[mask].mean()
            self.log(f"{prefix}/mnli_{sample_type.name.lower()}_loss", loss_per_type, on_step=False, on_epoch=True,
                     prog_bar=True, logger=True)
            self.log(f"{prefix}/mnli_{sample_type.name.lower()}_accuracy", acc_per_type, on_step=False, on_epoch=True,
                     prog_bar=True, logger=True)

    def _mnli_epoch_end(self, split: str, mnli_results):
        types = torch.cat([x["mnli_datapoint_type"] for x in mnli_results]).detach().cpu().numpy()
        losses = torch.cat([x["mnli_datapoint_loss"] for x in mnli_results]).detach().cpu().numpy()
        true_preds = torch.cat([x["mnli_true_preds"] for x in mnli_results]).detach().cpu().numpy()

        self._log_mnli_metrics_per_sample_type(split, types, losses, true_preds)
        self._log_loss_histogram(losses, split, "mnli_loss", log_df=False)

        # Create a DataFrame to be used in post-run logs processing to create visuals for the paper report
        mnli_df = pd.DataFrame({
            "type": types,
            "type_str": [SampleType(t).name.title() for t in types],
            "loss": losses,
            "true_preds": true_preds,
            "epoch": self.current_epoch,
            "step": self.global_step,
        })
        # Log the dataframe to wandb
        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger: WandbLogger = logger
                csv_path = os.path.join(
                    logger.experiment.dir,
                    f"{split}_mnli_epoch_end_df_epoch-{self.current_epoch}_step-{self.global_step}.csv"
                )
                mnli_df.to_csv(csv_path)
                artifact = wandb.Artifact(
                    name=f"{logger.experiment.name}-{split}-mnli_epoch_end_df",
                    type="df",
                    metadata={"epoch": self.current_epoch, "step": self.global_step},
                )
                artifact.add_file(csv_path, "df.csv")
                logger.experiment.log_artifact(artifact)

    def training_epoch_end(self, outputs):
        # MNLI
        mnli_results = outputs
        self._mnli_epoch_end("Train", mnli_results)

    def validation_epoch_end(self, outputs):
        # MNLI
        mnli_results = outputs[0]
        self._mnli_epoch_end("Valid", mnli_results)

        # HANS
        hans_results = outputs[1]

        preds = torch.cat([x["preds"] for x in hans_results]).detach().cpu().numpy()
        labels = torch.cat([x["labels"] for x in hans_results]).detach().cpu().numpy()
        heuristics = torch.cat([x["heuristic"] for x in hans_results]).detach().cpu().numpy()
        losses = torch.cat([x["hans_loss"] for x in hans_results]).detach().cpu().numpy()
        loss = losses.mean()

        acc = (preds == labels).sum() / len(preds)
        self.log("Valid/hans_loss", loss, on_step=False, on_epoch=True, prog_bar=True, logger=True)
        self.log("Valid/hans_acc", acc, on_step=False, on_epoch=True, prog_bar=True, logger=True)
        self.log("Valid/hans_count", float(len(preds)), on_step=False, on_epoch=True, prog_bar=True, logger=True)
        self._log_loss_histogram(losses, "Valid", "hans_loss", log_df=False)

        for target_label, label_description in enumerate(["entailment", "non_entailment"]):
            for heuristic_name, heuristic_idx in HEURISTIC_TO_INTEGER.items():
                mask = (heuristics == heuristic_idx) & (labels == target_label)
                if mask.sum() == 0:
                    # that way we avoid NaN and polluting our metrics
                    continue

                loss = losses[mask].mean()
                acc = (preds[mask] == labels[mask]).mean()
                self.log(f"Valid/Hans_loss/{label_description}_{heuristic_name}", loss, on_step=False, on_epoch=True,
                         prog_bar=True,
                         logger=True)
                self.log(f"Valid/Hans_acc/{label_description}_{heuristic_name}", acc, on_step=False, on_epoch=True,
                         prog_bar=True, logger=True)

        # Create a DataFrame to be used in post-run logs processing to create visuals for the paper report
        INTEGER_TO_HEURISTIC = {v: k.title().replace("_", " ") for k, v in HEURISTIC_TO_INTEGER.items()}
        hans_df = pd.DataFrame({
            "preds": preds,
            "labels": labels,
            "heuristics": heuristics,
            "heuristics_str": [INTEGER_TO_HEURISTIC[h] for h in heuristics],
            "losses": losses,
            "epoch": self.current_epoch,
            "step": self.global_step,
        })
        # Log the dataframe to wandb
        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger: WandbLogger = logger
                csv_path = os.path.join(
                    logger.experiment.dir,
                    f"Valid_hans_epoch_end_df_epoch-{self.current_epoch}_step-{self.global_step}.csv"
                )
                hans_df.to_csv(csv_path)
                artifact = wandb.Artifact(
                    name=f"{logger.experiment.name}-Valid-hans_epoch_end_df",
                    type="df",
                    metadata={"epoch": self.current_epoch, "step": self.global_step},
                )
                artifact.add_file(csv_path, "df.csv")
                logger.experiment.log_artifact(artifact)
        # # Log the dataframe to wandb
        # for logger in self.loggers:
        #     if isinstance(logger, WandbLogger):
        #         logger: WandbLogger = logger
        #         logger.experiment.log({f"Valid/Verbose/hans_epoch_end_df": hans_df})

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""

        model = self.bert
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "name": "1_w-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "name": "2_no-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]
        if self.hparams.optimizer_name == "adamw":
            optimizer = AdamW(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        elif self.hparams.optimizer_name == "adam":
            optimizer = Adam(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        else:
            raise ValueError(f"Invalid optimizer_name given: {self.hparams.optimizer_name}")

        train_steps = self.trainer.estimated_stepping_batches

        if self.hparams.warmup_ratio is not None and self.hparams.warmup_steps is not None:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but not both.")

        if self.hparams.warmup_steps:
            warmup_steps = self.hparams.warmup_steps
        elif self.hparams.warmup_ratio:
            warmup_steps = train_steps * self.hparams.warmup_ratio
        else:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but none were given.")

        if self.hparams.scheduler_name == "linear":
            scheduler = get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        elif self.hparams.scheduler_name == "polynomial":
            scheduler = get_polynomial_decay_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
                lr_end=0.0,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        else:
            raise ValueError(f"Invalid scheduler_name given: {self.hparams.optimizer_name}")

        return [optimizer], [scheduler]
