import numpy as np

from nowcast.model.metrics import contingency, csi


def motion_threshold(y, adv_max_pct) -> int:
    scores = [csi(*contingency(y, adv_max_pct, x)) or 0.0 for x in range(1, 61)]
    return int(np.argmax(scores)) + 1
