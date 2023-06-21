from dataclasses import asdict, dataclass
from typing import Literal

import torch
from einops import repeat
from torch import Tensor

from .accuracy import AccuracyResult, accuracy_ci
from .calibration import CalibrationError, CalibrationEstimate
from .roc_auc import RocAucResult, roc_auc_ci


@dataclass(frozen=True)
class EvalResult:
    """The result of evaluating a classifier."""

    accuracy: AccuracyResult
    """Top 1 accuracy, implemented for both binary and multi-class classification."""
    cal_accuracy: AccuracyResult | None
    """Calibrated accuracy, only implemented for binary classification."""
    calibration: CalibrationEstimate | None
    """Expected calibration error, only implemented for binary classification."""
    roc_auc: RocAucResult
    """Area under the ROC curve. For multi-class classification, each class is treated
    as a one-vs-rest binary classification problem."""

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        """Convert the result to a dictionary."""
        acc_dict = {f"{prefix}acc_{k}": v for k, v in asdict(self.accuracy).items()}
        cal_acc_dict = (
            {f"{prefix}cal_acc_{k}": v for k, v in asdict(self.cal_accuracy).items()}
            if self.cal_accuracy is not None
            else {}
        )
        cal_dict = (
            {f"{prefix}ece": self.calibration.ece}
            if self.calibration is not None
            else {}
        )
        auroc_dict = {f"{prefix}auroc_{k}": v for k, v in asdict(self.roc_auc).items()}
        return {**auroc_dict, **cal_acc_dict, **acc_dict, **cal_dict}


def calc_auroc(y_logits, y_true, ensembling, num_classes):
    if ensembling == "none":
        auroc = roc_auc_ci(
            to_one_hot(y_true, num_classes).long().flatten(1), y_logits.flatten(1)
        )
    elif ensembling in ("partial", "full"):
        # Pool together the negative and positive class logits
        if num_classes == 2:
            auroc = roc_auc_ci(y_true, y_logits[..., 1] - y_logits[..., 0])
        else:
            auroc = roc_auc_ci(to_one_hot(y_true, num_classes).long(), y_logits)
    else:
        raise ValueError(f"Unknown mode: {ensembling}")

    return auroc


def calc_calibrated_accuracies(y_true, pos_probs) -> AccuracyResult:
    """
    Calculate the calibrated accuracies

    Args:
        y_true: Ground truth tensor of shape (n,).
        pos_probs: Predicted class tensor of shape (n, num_variants, num_classes).

    Returns:
        AccuracyResult: A dictionary containing the accuracy and confidence interval.
    """

    cal_thresh = pos_probs.float().quantile(y_true.float().mean())
    cal_preds = pos_probs.gt(cal_thresh).to(torch.int)
    cal_acc = accuracy_ci(y_true, cal_preds)
    return cal_acc


def calc_calibrated_errors(y_true, pos_probs) -> CalibrationEstimate:
    """
    Calculate the expected calibration error.

    Args:
        y_true: Ground truth tensor of shape (n,).
        y_logits: Predicted class tensor of shape (n, num_variants, num_classes).

    Returns:
        CalibrationEstimate:
    """

    cal = CalibrationError().update(y_true.flatten(), pos_probs.flatten())
    cal_err = cal.compute()
    return cal_err


def calc_accuracies(y_logits, y_true) -> AccuracyResult:
    """
    Calculate the accuracy

    Args:
        y_true: Ground truth tensor of shape (n,).
        y_logits: Predicted class tensor of shape (n, num_variants, num_classes).

    Returns:
        AccuracyResult: A dictionary containing the accuracy and confidence interval.
    """
    y_pred = y_logits.argmax(dim=-1)
    return accuracy_ci(y_true, y_pred)


def evaluate_preds(
    y_true: Tensor,
    y_logits: Tensor,
    ensembling: Literal["none", "partial", "full"] = "none",
) -> EvalResult:
    """
    Evaluate the performance of a classification model.

    Args:
        y_true: Ground truth tensor of shape (n,).
        y_logits: Predicted class tensor of shape (n, num_variants, num_classes).

    Returns:
        dict: A dictionary containing the accuracy, AUROC, and ECE.
    """
    (n, num_variants, num_classes) = y_logits.shape
    assert y_true.shape == (n,)

    if ensembling == "full":
        y_logits = y_logits.mean(dim=1)
    else:
        y_true = repeat(y_true, "n -> n v", v=num_variants)

    return calc_eval_results(y_true, y_logits, ensembling, num_classes)


def calc_eval_results(y_true, y_logits, ensembling, num_classes) -> EvalResult:
    """
    Calculate the evaluation results

    Args:
        y_true: Ground truth tensor of shape (n,).
        y_logits: Predicted class tensor of shape (n, num_variants, num_classes).
        ensembling: The ensembling mode.

    Returns:
        EvalResult: The result of evaluating a classifier containing the accuracy,
        calibrated accuracies, calibrated errors, and AUROC.
    """
    acc = calc_accuracies(y_logits=y_logits, y_true=y_true)

    pos_probs = torch.sigmoid(y_logits[..., 1] - y_logits[..., 0])
    cal_acc = (
        calc_calibrated_accuracies(y_true=y_true, pos_probs=pos_probs)
        if num_classes == 2
        else None
    )
    cal_err = (
        calc_calibrated_errors(y_true=y_true, pos_probs=pos_probs)
        if num_classes == 2
        else None
    )

    auroc = calc_auroc(
        y_logits=y_logits, y_true=y_true, ensembling=ensembling, num_classes=num_classes
    )

    return EvalResult(acc, cal_acc, cal_err, auroc)


def layer_ensembling(layer_outputs) -> EvalResult:
    """
    Return EvalResult after ensembling the probe output of the middle to last layers

    Args:
        layer_outputs: A list of dictionaries containing the ground truth and
        predicted class tensor of shape (n, num_variants, num_classes).

    Returns:
        EvalResult: The result of evaluating a classifier containing the accuracy,
        calibrated accuracies, calibrated errors, and AUROC.
    """
    y_logits_means = []
    y_trues = []

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    for layer_output in layer_outputs:
        y_logits = layer_output[0]["val_credences"].to(device)

        # full ensembling
        y_logits_means.append(y_logits.mean(dim=1))

        y_true = layer_output[0]["val_gt"].to(device)
        y_trues.append(y_true)

    num_classes = layer_outputs[0][0]["val_credences"].shape[2]

    # get logits and ground_truth from middle to last layer
    middle_index = len(y_trues) // 2
    y_trues = y_trues[middle_index:]
    y_logits = y_logits_means[middle_index:]

    y_logits_layers = torch.stack(y_logits)

    # layer ensembling of the stacked logits
    y_layer_logits_means = torch.mean(y_logits_layers, dim=0)

    return calc_eval_results(
        y_true=y_trues[2],
        y_logits=y_layer_logits_means,
        ensembling="full",
        num_classes=num_classes,
    )


def to_one_hot(labels: Tensor, n_classes: int) -> Tensor:
    """
    Convert a tensor of class labels to a one-hot representation.

    Args:
        labels (Tensor): A tensor of class labels of shape (N,).
        n_classes (int): The total number of unique classes.

    Returns:
        Tensor: A one-hot representation tensor of shape (N, n_classes).
    """
    one_hot_labels = labels.new_zeros(*labels.shape, n_classes)
    return one_hot_labels.scatter_(-1, labels.unsqueeze(-1).long(), 1)
