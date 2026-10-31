"""Matching detections to ground truth, and labelling what did not match.

Class-agnostic, greedy by score, one ground-truth box per detection -- the
matching COCO uses for AP, minus the class condition. The class is *not*
required to match because the conformal gate's job is to say what a detected
object is; a detection that lands on a real plant but names the wrong species
is exactly a case the prediction set must cover, not one to discard.
"""
from __future__ import annotations

import numpy as np


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(N,4) x (M,4) xyxy -> (N,M) IoU."""
    a = np.asarray(a, np.float64).reshape(-1, 4)
    b = np.asarray(b, np.float64).reshape(-1, 4)
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.clip(aa[:, None] + ab[None, :] - inter, 1e-12, None)


def greedy_match(pred_boxes: np.ndarray, pred_scores: np.ndarray, gt_boxes: np.ndarray,
                 iou_thr: float = 0.5) -> np.ndarray:
    """For each prediction, the index of its ground-truth box, or -1.

    Highest score first; each ground-truth box is taken at most once, so a
    duplicate detection of the same plant is unmatched (it is a false positive
    and the gate should call it background).
    """
    n = len(pred_boxes)
    match = np.full(n, -1, np.int64)
    if n == 0 or len(gt_boxes) == 0:
        return match
    iou = iou_matrix(pred_boxes, gt_boxes)
    taken = np.zeros(len(gt_boxes), bool)
    for i in np.argsort(-np.asarray(pred_scores), kind="stable"):
        cand = np.where(~taken & (iou[i] >= iou_thr), iou[i], -1.0)
        j = int(np.argmax(cand))
        if cand[j] >= iou_thr:
            match[i] = j
            taken[j] = True
    return match


def label_detections(pred_boxes, pred_scores, gt_boxes, gt_labels, background_index: int,
                     iou_thr: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """(labels, match): ground-truth class for matched detections, background otherwise."""
    m = greedy_match(pred_boxes, pred_scores, gt_boxes, iou_thr)
    gl = np.asarray(gt_labels, np.int64)
    labels = np.where(m >= 0, gl[np.clip(m, 0, None)] if len(gl) else background_index,
                      background_index)
    return labels.astype(np.int64), m
