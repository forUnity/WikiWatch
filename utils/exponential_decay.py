import numpy as np

def exponential_decay(x: float, a: float):
    return np.exp(-a * x)

def get_exp_factor():
    num_seconds_normal = 1 * 365 * 24 * 60 *60
    return - np.log(0.5) / num_seconds_normal