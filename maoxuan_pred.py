import os
import re
import sys
import numpy as np
import tensorflow as tf

tf.config.threading.set_inter_op_parallelism_threads(2)
tf.config.threading.set_intra_op_parallelism_threads(12)

path = '/home/user/Jupyter/'
with open(path+'MaoXuan.txt','r') as f:
    text = f.read()

vocab = sorted(set(text))

char2idx = {u:i for i, u in enumerate(vocab)}
idx2char = np.array(vocab)

checkpoint_dir = path+'training_checkpoints'
tf.train.latest_checkpoint(checkpoint_dir)

def build_model(vocab_size, embedding_dim, rnn_units, batch_size):
    model = tf.keras.Sequential([
    tf.keras.layers.Embedding(vocab_size, embedding_dim,
                              batch_input_shape=[batch_size, None]),
    tf.keras.layers.GRU(rnn_units,
                        return_sequences=True,
                        stateful=True,
                        recurrent_initializer='glorot_uniform'),
    tf.keras.layers.Dense(vocab_size)
    ])
    return model
vocab_size = len(vocab)
embedding_dim = 256
rnn_units = 1024

model = build_model(vocab_size, embedding_dim, rnn_units, batch_size=1)
model.load_weights(tf.train.latest_checkpoint(checkpoint_dir))
model.build(tf.TensorShape([1, None]))
def generate_text(model, start_string, length=100):
    num_generate = int(length)
    input_eval = [char2idx[s] for s in start_string]
    input_eval = tf.expand_dims(input_eval, 0)
    text_generated = []
    temperature = 1.0
    model.reset_states()
    for i in range(num_generate):
        predictions = model(input_eval)
        predictions = tf.squeeze(predictions, 0)
        predictions = predictions / temperature
        predicted_id = tf.random.categorical(predictions, num_samples=1)[-1,0].numpy()
        input_eval = tf.expand_dims([predicted_id], 0)
        text_generated.append(idx2char[predicted_id])
    return (start_string + ''.join(text_generated))

def get_pred(content):
    try:
        op, num = content.split(' ')
    except:
        op = content
        num = 100
    gtext = generate_text(model, start_string=op, length=num)
    if '\n' in gtext:
        reply = ''.join(gtext.split('\n'))
        reply = '。'.join(reply.split('。')[:-1])+'。'
    else:
        reply = '。'.join(gtext.split('。')[:-1])+'。'
    return reply
