import numpy as np
def make_choice(content):
    question = content[0]
    choices = content[1:]
    np.random.shuffle(choices)
    string = question+'： '+choices[0]
    return string