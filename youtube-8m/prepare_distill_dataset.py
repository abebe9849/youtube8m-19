# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Binary for combine model output and model input into one set of files."""

import os
import time
import numpy as np

import tensorflow as tf
from tensorflow import app
from tensorflow import flags
from tensorflow import gfile
from tensorflow import logging
import json
from tensorflow.python.lib.io import file_io

import utils
import readers

FLAGS = flags.FLAGS

if __name__ == '__main__':
    flags.DEFINE_string("output_dir", "",
                        "The file to save the predictions to.")
    flags.DEFINE_string(
        "input_data_pattern", "",
        "File globs defining the input dataset in tensorflow.SequenceExample format.")
    flags.DEFINE_string("input_feature_names", "mean_rgb,mean_audio", "Name of the feature "
                                                                      "to use for training.")
    flags.DEFINE_string("input_feature_sizes", "1024,128", "Length of the feature vectors.")
    flags.DEFINE_string("prediction_feature_names", "predictions", "Name of the feature "
                                                                   "to use for training.")
    flags.DEFINE_integer("batch_size", 256,
                         "How many examples to process per batch.")
    flags.DEFINE_integer("file_size", 512,
                         "Number of samples per record file.")

    flags.DEFINE_string("model_file", "", "Seed model used to do inference.")

    flags.DEFINE_integer("num_readers", 1, #12
                         "How many threads to use for reading input files.")

def get_segments(batch_video_mtx, batch_num_frames, segment_size,labels_val):
    """Get segment-level inputs from frame-level features."""
    video_batch_size = batch_video_mtx.shape[0]
    max_frame = batch_video_mtx.shape[1]
    feature_dim = batch_video_mtx.shape[-1]
    padded_segment_sizes = (batch_num_frames + segment_size - 1) // segment_size
    padded_segment_sizes *= segment_size
    segment_mask = (
      0 < (padded_segment_sizes[:, np.newaxis] - np.arange(0, max_frame)))
    # print('segment_mask',segment_mask.shape)
    # Segment bags.
    frame_bags = batch_video_mtx.reshape((-1, feature_dim))
    segment_frames = frame_bags[segment_mask.reshape(-1)].reshape(
      (-1, segment_size, feature_dim))

    # Segment num frames.
    segment_start_times = np.arange(0, max_frame, segment_size)
    num_segments = batch_num_frames[:, np.newaxis] - segment_start_times
    num_segment_bags = num_segments.reshape((-1))
    valid_segment_mask = num_segment_bags > 0
    segment_num_frames = num_segment_bags[valid_segment_mask]
    segment_num_frames[segment_num_frames > segment_size] = segment_size

    max_segment_num = (max_frame + segment_size - 1) // segment_size
    video_idxs = np.tile(
      np.arange(0, video_batch_size)[:, np.newaxis], [1, max_segment_num])
    # print(np.arange(0, video_batch_size)[:, np.newaxis])
    # print(video_idxs)
    # print(labels_val.shape)
    # print(np.repeat(labels_val,max_segment_num,axis=0).shape)
    labels_stack=np.repeat(labels_val,max_segment_num,axis=0)

    segment_idxs = np.tile(segment_start_times, [video_batch_size, 1])
    idx_bags = np.stack([video_idxs, segment_idxs], axis=-1).reshape((-1, 2))
    video_segment_ids = idx_bags[valid_segment_mask]
    # print('idx_bags',idx_bags)
    # print(valid_segment_mask.shape)
    # print(labels_stack[valid_segment_mask,:].shape)
    # print('valid_segment_mask',valid_segment_mask[:10])
    # print(batch_num_frames)
    # print(batch_video_mtx[0,200:210,:])
    return {
      "video_batch": segment_frames,
      "num_frames_batch": segment_num_frames,
      "video_segment_ids": video_segment_ids,
      "labels_batch":labels_stack[valid_segment_mask,:]
    }

def get_input_data_tensors(reader,
                                 data_pattern,
                                 batch_size=1024,
                                 num_readers=1):
    """Creates the section of the graph which reads the evaluation data.

    Args:
      reader: A class which parses the training data.
      data_pattern: A 'glob' style path to the data files.
      batch_size: How many examples to process at a time.
      num_readers: How many I/O threads to use.

    Returns:
      A tuple containing the features tensor, labels tensor, and optionally a
      tensor containing the number of frames per video. The exact dimensions
      depend on the reader being used.

    Raises:
      IOError: If no files matching the given pattern were found.
    """
    # logging.info("Using batch size of " + str(batch_size) + " for evaluation.")
    with tf.name_scope("input"):
        files = gfile.Glob(data_pattern)
        if not files:
            raise IOError("Unable to find the evaluation files.")
        logging.info("number of evaluation files: " + str(len(files)))
        filename_queue = tf.train.string_input_producer(
            files, shuffle=False, num_epochs=1)
        eval_data = [
            reader.prepare_reader(filename_queue) for _ in range(num_readers)
            ]
        print('eval_data',eval_data)
        input_data_dict = (
            tf.train.batch_join(
                eval_data,
                batch_size=batch_size,
                allow_smaller_final_batch=True,
                enqueue_many=True))#capacity=4 * batch_size,
        video_id_batch = input_data_dict["video_ids"]
        model_input_raw = input_data_dict["video_matrix"]
        labels_batch = input_data_dict["labels"]
        num_frames = input_data_dict["num_frames"]
        return video_id_batch,model_input_raw,labels_batch,num_frames#
        # return tf.train.batch_join(
        #     eval_data,
        #     batch_size=batch_size,
        #     capacity=4 * batch_size,
        #     allow_smaller_final_batch=True,
        #     enqueue_many=True)

# Prepare the inputs
def fetc_inputs(reader,
                eval_data_pattern,
                batch_size=1024,
                num_readers=1):

    video_id_batch, model_input_raw, labels_batch, num_frames= get_input_data_tensors(reader,
                                                                                    eval_data_pattern,
                                                                                    batch_size=batch_size,
                                                                                    num_readers=num_readers)
    # print('video_id',video_id_batch)
    return video_id_batch, model_input_raw, labels_batch, num_frames


# Builds the record strucutre
def get_output_feature(video_id, video_label, video_rgb, video_audio, video_num_frame):# video_prediction,
    _bytes_feature_list = lambda x: tf.train.Feature(bytes_list=tf.train.BytesList(value=[x.tobytes()]))
    example = tf.train.SequenceExample(
        context = tf.train.Features(feature={
            "id": tf.train.Feature(bytes_list=tf.train.BytesList(value=[video_id])),
            "labels": tf.train.Feature(int64_list=tf.train.Int64List(value=video_label)),
            # "predictions": tf.train.Feature(float_list=tf.train.FloatList(value=video_prediction))
        }),
        feature_lists = tf.train.FeatureLists(feature_list={
            "rgb": tf.train.FeatureList(feature=map(_bytes_feature_list, video_rgb[:video_num_frame])),
            "audio": tf.train.FeatureList(feature=map(_bytes_feature_list, video_audio[:video_num_frame])),
        })
    )
    return example


# Write the records
def write_to_record(video_ids, video_labels, video_rgbs, video_audios,
                    video_num_frames, filenum, num_examples_processed):#, video_predictions
    writer = tf.python_io.TFRecordWriter(FLAGS.output_dir + '/' + 'predictions%05d.tfrecord' % filenum)
    for i in range(num_examples_processed):
        video_id = video_ids[i]
        video_label = np.nonzero(video_labels[i,:])[0]
        # print('video_label',video_label)
        video_rgb = video_rgbs[i,:]
        video_audio = video_audios[i,:]
        # video_prediction = video_predictions[i]
        video_num_frame = video_num_frames[i]
        example = get_output_feature(video_id, video_label, video_rgb, video_audio, video_num_frame)#video_prediction,
        serialized = example.SerializeToString()
        writer.write(serialized)
    writer.close()


def inference_loop():
    model_path = FLAGS.model_file
    checkpoint_file = os.path.join(model_path, "inference_model",
                                   "inference_model")
    print(checkpoint_file)
    # assert os.path.isfile(checkpoint_file + ".meta"), "Specified model does not exist."
    if not gfile.Exists(checkpoint_file + ".meta"):
        raise IOError("Cannot find %s. Did you run eval.py?" % checkpoint_file)

    # model_flags_path = os.path.join(os.path.dirname(model_path), "model_flags.json")
    model_flags_path = os.path.join(model_path, "model_flags.json")

    directory = FLAGS.output_dir  # We will store the predictions here.
    if not os.path.exists(directory):
        os.makedirs(directory)
    else:
        raise IOError("Output path exists! path='" + directory + "'")

    # if not gfile.Exists(model_flags_path):#os.path.exists(model_flags_path):
    #     raise IOError(("Cannot find file %s. Did you run train.py on the same "
    #                    "--train_dir?") % model_flags_path)
    # flags_dict = json.loads(open(model_flags_path).read())

    if not file_io.file_exists(model_flags_path):
        raise IOError("Cannot find %s. Did you run eval.py?" % model_flags_path)
    flags_dict = json.loads(file_io.FileIO(model_flags_path, "r").read())

    feature_names, feature_sizes = utils.GetListOfFeatureNamesAndSizes(flags_dict["feature_names"],
                                                                       flags_dict["feature_sizes"])
    if flags_dict["frame_features"]:
        reader = readers.YT8MFrameFeatureReader(feature_names=feature_names,
                                                feature_sizes=feature_sizes)

    else:
        raise NotImplementedError
    video_ids_batch, inputs_batch, labels_batch, num_frames= fetc_inputs(reader,
                                                                            FLAGS.input_data_pattern,
                                                                            FLAGS.batch_size,
                                                                            FLAGS.num_readers)

    with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:
        # video_id_batch, video_batch, num_frames_batch = get_input_data_tensors(
        # reader, FLAGS.input_data_pattern, FLAGS.batch_size)
        # meta_graph_location = model_path + ".meta"
        meta_graph_location = checkpoint_file + ".meta"
        logging.info("loading meta-graph: " + meta_graph_location)
        with tf.device("/cpu:0"):#gpu
            saver = tf.train.import_meta_graph(meta_graph_location, clear_devices=True)
            saver.restore(sess, checkpoint_file)

        input_tensor = tf.get_collection("input_batch_raw")[0]
        num_frames_tensor = tf.get_collection("num_frames")[0]
        predictions_batch = tf.get_collection("predictions")[0]

        # Workaround for num_epochs issue.
        def set_up_init_ops(variables):
            init_op_list = []
            for variable in list(variables):
                if "train_input" in variable.name:
                    init_op_list.append(tf.assign(variable, 1))
                    variables.remove(variable)
            init_op_list.append(tf.variables_initializer(variables))
            return init_op_list

        sess.run(set_up_init_ops(tf.get_collection_ref(tf.GraphKeys.LOCAL_VARIABLES)))

        # Start the queue runners.
        fetches1 = [video_ids_batch, labels_batch, inputs_batch, num_frames]
        fetches2 = [predictions_batch]
        coord = tf.train.Coordinator()
        start_time = time.time()

        video_ids = []
        video_labels = []
        video_rgbs = []
        video_audios = []
        video_predictions = []
        video_num_frames = []
        filenum = 0

        num_examples_processed = 0
        total_num_examples_processed = 0

        import csv
        import urllib2
        # import numpy as np
        whitelisted_cls_mask = np.zeros((3862,),
                                    dtype=np.float32)
        url = 'http://storage.googleapis.com/youtube8m-lijun-mlengine/segment_label_ids.csv'
        response = urllib2.urlopen(url)
        fobj = csv.reader(response)
        for line in fobj:
            try:
              cls_id = int(line[0])
              whitelisted_cls_mask[cls_id] = 1.
            except ValueError:
                # Simply skip the non-integer line.
              continue  
        response.close()
        # print('mask',whitelisted_cls_mask)
        # print('mask_shape',whitelisted_cls_mask.shape)

        try:
            threads = []
            for qr in tf.get_collection(tf.GraphKeys.QUEUE_RUNNERS):
                threads.extend(qr.create_threads(
                    sess, coord=coord, daemon=True,
                    start=True))

            while not coord.should_stop():
                ids_val = None
                ids_val, labels_val, inputs_val, num_frames_val = sess.run(fetches1)
                # print('labels_val',labels_val.shape)
                target=np.matmul(labels_val,whitelisted_cls_mask)
                # print('target',target.shape)
                target_index=target>0.
                labels_val=labels_val[target_index,:]
                inputs_val=inputs_val[target_index,:,:]
                num_frames_val=num_frames_val[target_index]
                ids_val=ids_val[target_index]


                results = get_segments(inputs_val[:,20:,:], num_frames_val-20-20, 5,labels_val)
                results["video_segment_ids"][:,1]=results["video_segment_ids"][:,1]+20
                video_segment_ids = results["video_segment_ids"]
                video_id_batch_val = ids_val[video_segment_ids[:, 0]]
                video_id_batch_val = np.array([
                  "%s:%d" % (x, y)
                  for x, y in zip(video_id_batch_val, video_segment_ids[:, 1])
                ])
                ids_val=video_id_batch_val
                inputs_val=results['video_batch']
                num_frames_val=results['num_frames_batch']
                labels_val=results['labels_batch']

                # print(results['video_batch'].shape)
                # print(results['video_segment_ids'])
                # print(results['num_frames_batch'])
                print(video_id_batch_val)


                # rgbs_val, audios_val = quant_inpt_val[:, :, :1024].copy(), quant_inpt_val[:, :, 1024:].copy()
                rgbs_val, audios_val = inputs_val[:, :, :1024].copy(), inputs_val[:, :, 1024:].copy()

                predictions_val = sess.run(fetches2, feed_dict={input_tensor: inputs_val,
                                                                num_frames_tensor: num_frames_val})[0]

                # print('inputs_val',inputs_val)
                # print('inputs_val',inputs_val.shape)
                # print('num_frames',num_frames_val)
                # print('num_frames',num_frames_val.shape)
                # print('ids_val',ids_val)
                # print('ids_val',ids_val.shape)
                # print('labels_val',labels_val)
                # print('labels_val',labels_val.shape)
                # print('predictions_val',predictions_val)
                # print('predictions_val',predictions_val.shape)
                # print('predictions_val',predictions_val.shape)
                # print('whitelisted_cls_mask sum',np.sum(whitelisted_cls_mask))
                predictions_val=predictions_val*whitelisted_cls_mask

                video_max=np.amax(predictions_val,axis=1)
                sorted_idx=np.argsort(-video_max)[:64]
                # print('index',video_max[sorted_idx])
                maxIndex=np.argmax(predictions_val,axis=1)
                select=labels_val[np.arange(len(labels_val)),maxIndex]

                if len(ids_val[select])>=64:#256
                    ids_val=ids_val[select]
                    labels_val=labels_val[select,:]
                    rgbs_val=rgbs_val[select,:,:]
                    audios_val=audios_val[select,:,:]
                    predictions_val=predictions_val[select,:]
                    num_frames_val=num_frames_val[select]
                    inputs_val=inputs_val[select,:,:]
                    video_max=np.amax(predictions_val,axis=1)
                    sorted_idx=np.argsort(-video_max)[:64]
                    print('saved segments are truly predicted')

                ids_val=ids_val[sorted_idx]
                labels_val=labels_val[sorted_idx,:]
                rgbs_val=rgbs_val[sorted_idx,:,:]
                audios_val=audios_val[sorted_idx,:,:]
                predictions_val=predictions_val[sorted_idx,:]
                num_frames_val=num_frames_val[sorted_idx]

                # video_max_matrix=np.repeat(np.amax(predictions_val,axis=1)[:,np.newaxis],3862,axis=1)
                predictions_val=predictions_val>=(np.ones_like(predictions_val)*0.5)#video_max_matrix
                # video_max_matrix=np.repeat(np.amax(predictions_val,axis=1)[:,np.newaxis],3862,axis=1)
                # predictions_val=predictions_val>=video_max_matrix

                # print('boolean_prediction',predictions_val)
                # print(predictions_val.shape)

                video_ids.append(ids_val)#ids_val
                video_labels.append(labels_val)#labels_val
                video_rgbs.append(rgbs_val)#rgbs_val
                video_audios.append(audios_val)#audios_val
                video_predictions.append(predictions_val)#predictions_val
                video_num_frames.append(num_frames_val)#num_frames_val
                num_examples_processed += len(ids_val)

                ids_shape = ids_val.shape[0]
                inputs_shape = inputs_val[sorted_idx,:,:].shape[0]
                predictions_shape = predictions_val.shape[0]
                assert ids_shape == inputs_shape == predictions_shape, "tensor ids(%d), inputs(%d) and predictions(%d) should have equal rows" % (
                    ids_shape, inputs_shape, predictions_shape)

                ids_val = None

                if num_examples_processed >= FLAGS.file_size:
                    assert num_examples_processed == FLAGS.file_size, "num_examples_processed should be equal to %d" % FLAGS.file_size
                    video_ids = np.concatenate(video_ids, axis=0)
                    # video_labels = np.concatenate(video_labels, axis=0)
                    video_labels = np.concatenate(video_predictions, axis=0)
                    video_rgbs = np.concatenate(video_rgbs, axis=0)
                    video_audios = np.concatenate(video_audios, axis=0)
                    video_num_frames = np.concatenate(video_num_frames, axis=0)
                    video_predictions = np.concatenate(video_predictions, axis=0)
                    write_to_record(video_ids, video_labels, video_rgbs, video_audios,
                                    video_num_frames, filenum, num_examples_processed)#, video_predictions

                    video_ids = []
                    video_labels = []
                    video_rgbs = []
                    video_audios = []
                    video_predictions = []
                    video_num_frames = []
                    filenum += 1
                    total_num_examples_processed += num_examples_processed

                    now = time.time()
                    logging.info("num examples processed: " + str(
                        total_num_examples_processed) + " elapsed seconds: " + "{0:.2f}".format(now - start_time))
                    num_examples_processed = 0

        except tf.errors.OutOfRangeError as e:
            if ids_val is not None:
                video_ids.append(ids_val)
                video_labels.append(labels_val)
                video_rgbs.append(rgbs_val)
                video_audios.append(audios_val)
                video_predictions.append(predictions_val)
                video_num_frames.append(num_frames_val)

                num_examples_processed += len(ids_val)

            if 0 < num_examples_processed <= FLAGS.file_size:
                video_ids = np.concatenate(video_ids, axis=0)
                # video_labels = np.concatenate(video_labels, axis=0)
                video_labels = np.concatenate(video_predictions, axis=0)
                video_rgbs = np.concatenate(video_rgbs, axis=0)
                video_audios = np.concatenate(video_audios, axis=0)
                video_num_frames = np.concatenate(video_num_frames, axis=0)
                video_predictions = np.concatenate(video_predictions, axis=0)
                write_to_record(video_ids, video_labels, video_rgbs, video_audios,
                                video_num_frames, filenum, num_examples_processed)#, video_predictions

                total_num_examples_processed += num_examples_processed

                now = time.time()
                logging.info(
                    "num examples processed: " + str(total_num_examples_processed) + " elapsed seconds: " + "{0:.2f}".format(
                        now - start_time))

            logging.info(
                "Done with inference. %d samples was written to %s" % (total_num_examples_processed, FLAGS.output_dir))
        # except Exception as e:  # pylint: disable=broad-except
        #     logging.info("Unexpected exception: " + str(e))
        finally:
            coord.request_stop()

        coord.join(threads, stop_grace_period_secs=10)

def main(unused_argv):
    logging.set_verbosity(tf.logging.INFO)
    inference_loop()

if __name__ == "__main__":
    app.run()
