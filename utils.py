import os
import io
import random
import pickle
import hashlib
import numpy as np
from tqdm import tqdm
import tensorflow as tf
import sentencepiece as spm
import tensorflow_hub as hub
from annoy import AnnoyIndex

# Specify the local module path
MODULE_PATH = 'https://tfhub.dev/google/universal-sentence-encoder-lite/2'


def save_obj(obj, name):
    with open(name, 'wb') as f:
        pickle.dump(obj, f, pickle.HIGHEST_PROTOCOL)

def load_obj(name):
    with open(name, 'rb') as f:
        return pickle.load(f)

def load_data(file_path, dest_type):
  """Load line separated text files into list. """
  if dest_type == 'list':
    tempfile = io.open(file_path, 'r', encoding="utf-8", errors='ignore')
    dest = []
    for line in tempfile:
      clean_string = line.strip()
      # check if blank
      if clean_string:
        dest.append(clean_string)
    tempfile.close()
  elif dest_type == 'dict':
    dest = load_obj(file_path)
  else:
    dest = None
    print('Bad destination data type specified.')
  return dest

def process_to_IDs_in_sparse_format(sp, sentences):
  # An utility method that processes sentences with the sentence piece processor
  # 'sp' and returns the results in tf.SparseTensor-similar format:
  # (values, indices, dense_shape)
  ids = [sp.EncodeAsIds(x) for x in sentences]
  max_len = max(len(x) for x in ids)
  dense_shape=(len(ids), max_len)
  values=[item for sublist in ids for item in sublist]
  indices=[[row,col] for row in range(len(ids)) for col in range(len(ids[row]))]
  return (values, indices, dense_shape)

def chunks(the_big_list, n_sub_list):
  """Yield successive n_sub_list-sized chunks from the_big_list."""
  for i in range(0, len(the_big_list), n_sub_list):
    yield the_big_list[i:i + n_sub_list]

def embed_lines(args, unencoded_lines, output_dict):
  # Import the Universal Sentence Encoder's TF Hub module

  module = hub.Module(MODULE_PATH, trainable=False)

  config = tf.ConfigProto(allow_soft_placement = True)
  # sess = tf.Session(config = config)

  with tf.Session(config = config) as session:
    # initialize the variables
    session.run([tf.global_variables_initializer(), tf.tables_initializer()])

    # spm_path now contains a path to the SentencePiece
    # model stored inside the TF-Hub module
    spm_path = session.run(module(signature="spm_path"))
    sp = spm.SentencePieceProcessor()
    sp.Load(spm_path)

    # build an input placeholder
    with tf.device('/gpu:0'):
      input_placeholder = tf.sparse_placeholder(tf.int64, shape=[None, None])
      embeddings = module(inputs=dict(
        values=input_placeholder.values,
        indices=input_placeholder.indices,
        dense_shape=input_placeholder.dense_shape
      	)
      )

    # size of chunk is how many lines will be encoded
    # with each pass of the model
    size_of_chunk = 256
    all_chunks = chunks(unencoded_lines, size_of_chunk)

    for chunk_unencoded_lines in tqdm(all_chunks, total=(len(unencoded_lines) // size_of_chunk)):
      # process unencoded lines to values and IDs in sparse format
      values, indices, dense_shape = process_to_IDs_in_sparse_format(sp=sp,
        sentences=chunk_unencoded_lines)

      # run the session
      chunk_line_embeddings = session.run(
        embeddings,
        feed_dict={
          input_placeholder.values: values,
          input_placeholder.indices: indices,
          input_placeholder.dense_shape: dense_shape
        }
      )

      # output logs if verbose and hash the object into the full output dataframe
      for i, line_embedding in enumerate(np.array(chunk_line_embeddings).tolist()):
        if args.verbose:
          tf.logging.log(tf.logging.INFO, "Line: {}".format(chunk_unencoded_lines[i]))
          tf.logging.log(tf.logging.INFO, "Embedding size: {}".format(len(line_embedding)))
          line_embedding_snippet = ", ".join((str(x) for x in line_embedding[:3]))
          tf.logging.log(tf.logging.INFO, "Embedding: [{}, ...]\n".format(line_embedding_snippet))

        # Encode a hash for the string
        hash_object = hashlib.md5(chunk_unencoded_lines[i].encode('utf-8'))
        # Add a row to the dataframe
        output_dict[hash_object.hexdigest()] = {'line': chunk_unencoded_lines[i],
                                                'line_embedding': line_embedding}
  return output_dict

class GenModelUSE():

    def __init__(self, annoy_index_path, unique_strings):
        self.annoy_index_path = annoy_index_path
        self.unique_strings = unique_strings

        # load the annoy index for mmap speed
        # Length of item vector that will be indexed
        self.annoy_index = AnnoyIndex(512)

        # super fast, will just mmap the file
        self.annoy_index.load(self.annoy_index_path)

        g = tf.Graph()
        with g.as_default():
          # define the module
          module = hub.Module(MODULE_PATH, trainable=False)
          # build an input placeholder
          self.input_placeholder = tf.sparse_placeholder(tf.int64, shape=[None, None])
          # build an input / output from the placeholders
          self.embeddings = module(inputs=dict(
              values=self.input_placeholder.values,
              indices=self.input_placeholder.indices,
              dense_shape=self.input_placeholder.dense_shape
            )
          )
          init_op = tf.group([tf.global_variables_initializer(), tf.tables_initializer()])

        # do not finalize the graph as we are going to modify it with the spm_path
        # g.finalize()
        config = tf.ConfigProto(allow_soft_placement = True)
        self.sess = tf.Session(graph=g, config=config)
        self.sess.run(init_op)

        # spm_path now contains a path to the SentencePiece
        # model stored inside the TF-Hub module
        with g.as_default():
          spm_path = self.sess.run(module(signature="spm_path"))
        self.sp = spm.SentencePieceProcessor()
        self.sp.Load(spm_path)

        tf.logging.info('Interactive session is initialized...')

    def inference(self, input_text):
        """Inference from nearest neighbor model."""
        tf.logging.info('Input text: {}'.format(input_text))

        # Build a list of the user input
        user_input = [input_text]

        # process unencoded lines to values and IDs in sparse format
        values, indices, dense_shape = process_to_IDs_in_sparse_format(sp=self.sp,
          sentences=user_input)

        # run the session
        # Get embedding of the input text
        embeddings = self.sess.run(
          self.embeddings,
          feed_dict={
            self.input_placeholder.values: values,
            self.input_placeholder.indices: indices,
            self.input_placeholder.dense_shape: dense_shape
          }
        )

        tf.logging.info('Successfully generated {} embeddings of length {}.'.format(len(embeddings),
            len(embeddings[0])))

        # Extract the query vector of interest.
        query_vector = embeddings[0]

        # Get nearest neighbors
        nns = self.annoy_index.get_nns_by_vector(query_vector, 3,
            search_k=-1, include_distances=False)
        tf.logging.info('Nearest neighbor IDS: {}'.format(nns))

        # Randomly sample from the top-3 nearest neighbors to avoid determinism
        generative_response = self.unique_strings[random.choice(nns)]

        return generative_response
