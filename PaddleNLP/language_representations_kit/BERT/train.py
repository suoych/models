#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT pretraining."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import argparse
import numpy as np
import multiprocessing

import paddle
import paddle.fluid as fluid
import random

#import reader_daxiang.pre_sampled_pretraining as psp
from reader.pretraining import DataReader
from model.bert import BertModel, BertConfig
from optimization import optimization
from utils.args import ArgumentGroup, print_arguments, check_cuda
from utils.init import init_checkpoint, init_pretraining_params

# yapf: disable
parser = argparse.ArgumentParser(__doc__)
model_g = ArgumentGroup(parser, "model", "model configuration and paths.")
model_g.add_arg("bert_config_path",      str,  "./config/bert_config.json",  "Path to the json file for bert model config.")
model_g.add_arg("init_checkpoint",       str,  None,                         "Init checkpoint to resume training from.")
model_g.add_arg("checkpoints",           str,  "checkpoints",                "Path to save checkpoints.")
model_g.add_arg("weight_sharing",        bool, True,                         "If set, share weights between word embedding and masked lm.")
model_g.add_arg("generate_neg_sample",   bool, True,                         "If set, randomly generate negtive samples by positive samples.")

train_g = ArgumentGroup(parser, "training", "training options.")
train_g.add_arg("epoch",             int,    100,     "Number of epoches for training.")
train_g.add_arg("learning_rate",     float,  0.0001,  "Learning rate used to train with warmup.")
train_g.add_arg("lr_scheduler",      str,    "linear_warmup_decay",
                "scheduler of learning rate.", choices=['linear_warmup_decay', 'noam_decay'])
train_g.add_arg("weight_decay",      float,  0.01,    "Weight decay rate for L2 regularizer.")
train_g.add_arg("num_train_steps",   int,    1000000, "Total steps to perform pretraining.")
train_g.add_arg("warmup_steps",      int,    4000,    "Total steps to perform warmup when pretraining.")
train_g.add_arg("save_steps",        int,    10000,   "The steps interval to save checkpoints.")
train_g.add_arg("validation_steps",  int,    1000,    "The steps interval to evaluate model performance.")
train_g.add_arg("use_fp16",          bool,   False,   "Whether to use fp16 mixed precision training.")
train_g.add_arg("loss_scaling",      float,  1.0,
                "Loss scaling factor for mixed precision training, only valid when use_fp16 is enabled.")

log_g = ArgumentGroup(parser,     "logging", "logging related.")
log_g.add_arg("skip_steps",          int,    10,    "The steps interval to print loss.")
log_g.add_arg("verbose",             bool,   False, "Whether to output verbose log.")

data_g = ArgumentGroup(parser, "data", "Data paths, vocab paths and data processing options")
data_g.add_arg("data_dir",            str,  "./data/train/",       "Path to training data.")
data_g.add_arg("validation_set_dir",  str,  "./data/validation/",  "Path to validation data.")
data_g.add_arg("test_set_dir",        str,  None,                  "Path to test data.")
data_g.add_arg("vocab_path",          str,  "./config/vocab.txt",  "Vocabulary path.")
data_g.add_arg("max_seq_len",         int,  512,                   "Tokens' number of the longest seqence allowed.")
data_g.add_arg("batch_size",          int,  8192,
               "The total number of examples in one batch for training, see also --in_tokens.")
data_g.add_arg("in_tokens",           bool, True,
               "If set, the batch size will be the maximum number of tokens in one batch. "
               "Otherwise, it will be the maximum number of examples in one batch.")

run_type_g = ArgumentGroup(parser, "run_type", "running type options.")
run_type_g.add_arg("is_distributed",               bool,   False,  "If set, then start distributed training.")
run_type_g.add_arg("use_cuda",                     bool,   True,   "If set, use GPU for training.")
run_type_g.add_arg("use_fast_executor",            bool,   False,  "If set, use fast parallel executor (in experiment).")
run_type_g.add_arg("num_iteration_per_drop_scope", int,    1,      "Ihe iteration intervals to clean up temporary variables.")
run_type_g.add_arg("do_test",                      bool,   False,  "Whether to perform evaluation on test data set.")

args = parser.parse_args()
# yapf: enable.


def create_model(bert_config):
    input_fields = {
        'names': ['src_ids', 'pos_ids', 'sent_ids', 'input_mask', 'mask_label', 'mask_pos', 'labels'],
        'shapes': [[-1, args.max_seq_len, 1], [-1, args.max_seq_len, 1],
                [-1, args.max_seq_len, 1],
                [-1, args.max_seq_len, 1], [-1, 1], [-1, 1], [-1, 1]],
        'dtypes': ['int64', 'int64', 'int64', 'float32', 'int64', 'int64', 'int64'],
        'lod_levels': [0, 0, 0, 0, 0, 0, 0],
    }

    inputs = [fluid.layers.data(name=input_fields['names'][i],
                      shape=input_fields['shapes'][i],
                      dtype=input_fields['dtypes'][i],
                      lod_level=input_fields['lod_levels'][i]) for i in range(len(input_fields['names']))]

    (src_ids, pos_ids, sent_ids, input_mask, mask_label, mask_pos, labels) = inputs

    pyreader = fluid.io.PyReader(feed_list=inputs, capacity=50, iterable=False)

    bert = BertModel(
        src_ids=src_ids,
        position_ids=pos_ids,
        sentence_ids=sent_ids,
        input_mask=input_mask,
        config=bert_config,
        weight_sharing=args.weight_sharing,
        use_fp16=args.use_fp16)

    next_sent_acc, mask_lm_loss, total_loss = bert.get_pretraining_output(
        mask_label, mask_pos, labels)

    if args.use_fp16 and args.loss_scaling > 1.0:
        total_loss *= args.loss_scaling
    bert.checkpoints = inputs + bert.checkpoints
    return pyreader, next_sent_acc, mask_lm_loss, total_loss, bert.checkpoints


def predict_wrapper(args,
                    exe,
                    bert_config,
                    test_prog=None,
                    pyreader=None,
                    fetch_list=None):
    # Context to do validation.
    data_path = args.test_set_dir if args.do_test else args.validation_set_dir
    data_reader = DataReader(
        data_path,
        vocab_path=args.vocab_path,
        batch_size=args.batch_size,
        in_tokens=args.in_tokens,
        voc_size=bert_config['vocab_size'],
        shuffle_files=False,
        epoch=1,
        max_seq_len=args.max_seq_len,
        is_test=True)

    pyreader.decorate_batch_generator(data_reader.data_generator())

    if args.do_test:
        assert args.init_checkpoint is not None, "[FATAL] Please use --init_checkpoint '/path/to/checkpoints' \
                                                  to specify you pretrained model checkpoints"

        init_pretraining_params(exe, args.init_checkpoint, test_prog)

    def predict(exe=exe, pyreader=pyreader):
        pyreader.start()

        cost = 0
        lm_cost = 0
        acc = 0
        steps = 0
        time_begin = time.time()
        while True:
            try:
                each_next_acc, each_mask_lm_cost, each_total_cost = exe.run(
                    fetch_list=fetch_list, program=test_prog)
                acc += each_next_acc
                lm_cost += each_mask_lm_cost
                cost += each_total_cost
                steps += 1
                if args.do_test and steps % args.skip_steps == 0:
                    print("[test_set] steps: %d" % steps)

            except fluid.core.EOFException:
                pyreader.reset()
                break

        used_time = time.time() - time_begin
        return cost, lm_cost, acc, steps, (args.skip_steps / used_time)

    return predict


def test(args):
    bert_config = BertConfig(args.bert_config_path)
    bert_config.print_config()

    test_prog = fluid.Program()
    test_startup = fluid.Program()
    with fluid.program_guard(test_prog, test_startup):
        with fluid.unique_name.guard():
            test_pyreader, next_sent_acc, mask_lm_loss, total_loss = create_model(
                bert_config=bert_config)

    test_prog = test_prog.clone(for_test=True)

    place = fluid.CUDAPlace(0) if args.use_cuda == True else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(test_startup)

    predict = predict_wrapper(
        args,
        exe,
        bert_config,
        test_prog=test_prog,
        pyreader=test_pyreader,
        fetch_list=[next_sent_acc.name, mask_lm_loss.name, total_loss.name])

    print("test begin")
    loss, lm_loss, acc, steps, speed = predict()
    print(
        "[test_set] loss: %f, global ppl: %f, next_sent_acc: %f, speed: %f steps/s"
        % (np.mean(np.array(loss) / steps),
           np.exp(np.mean(np.array(lm_loss) / steps)),
           np.mean(np.array(acc) / steps), speed))


def train(args):
    print("pretraining start")
    bert_config = BertConfig(args.bert_config_path)
    bert_config.print_config()

    train_program = fluid.Program()
    startup_prog = fluid.Program()
    train_program.random_seed = 9000
    startup_prog.random_seed = 9000
    random.seed(9000)
    with fluid.program_guard(train_program, startup_prog):
        with fluid.unique_name.guard():
            train_pyreader, next_sent_acc, mask_lm_loss, total_loss, checkpoints = create_model(
                bert_config=bert_config)
            scheduled_lr = optimization(
                loss=total_loss,
                warmup_steps=args.warmup_steps,
                num_train_steps=args.num_train_steps,
                learning_rate=args.learning_rate,
                train_program=train_program,
                startup_prog=startup_prog,
                weight_decay=args.weight_decay,
                checkpoints=checkpoints,
                scheduler=args.lr_scheduler,
                use_fp16=args.use_fp16,
                loss_scaling=args.loss_scaling)

    test_prog = fluid.Program()
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            test_pyreader, next_sent_acc, mask_lm_loss, total_loss, checkpoints = create_model(
                bert_config=bert_config)

    test_prog = test_prog.clone(for_test=True)

    if args.use_cuda:
        place = fluid.CUDAPlace(0)
        dev_count = fluid.core.get_cuda_device_count()
    else:
        place = fluid.CPUPlace()
        dev_count = int(os.environ.get('CPU_NUM', multiprocessing.cpu_count()))

    print("Device count %d" % dev_count)
    if args.verbose:
        if args.in_tokens:
            lower_mem, upper_mem, unit = fluid.contrib.memory_usage(
         program=train_program,
         batch_size=args.batch_size // args.max_seq_len)
        else:
            lower_mem, upper_mem, unit = fluid.contrib.memory_usage(
         program=train_program, batch_size=args.batch_size)
        print("Theoretical memory usage in training: %.3f - %.3f %s" %
              (lower_mem, upper_mem, unit))

    nccl2_num_trainers = 1
    nccl2_trainer_id = 0
    print("args.is_distributed:", args.is_distributed)
    if args.is_distributed:
        worker_endpoints_env = os.getenv("worker_endpoints")
        worker_endpoints = worker_endpoints_env.split(",")
        trainers_num = len(worker_endpoints)
        current_endpoint = os.getenv("current_endpoint")
        trainer_id = worker_endpoints.index(current_endpoint)
        if trainer_id == 0:
            print("train_id == 0, sleep 60s")
            time.sleep(60)
        print("worker_endpoints:{} trainers_num:{} current_endpoint:{} \
              trainer_id:{}"
                            .format(worker_endpoints, trainers_num,
                                    current_endpoint, trainer_id))

        # prepare nccl2 env.
        config = fluid.DistributeTranspilerConfig()
        config.mode = "nccl2"
        t = fluid.DistributeTranspiler(config=config)
        t.transpile(
            trainer_id,
            trainers=worker_endpoints_env,
            current_endpoint=current_endpoint,
            program=train_program,
            startup_program=startup_prog)
        nccl2_num_trainers = trainers_num
        nccl2_trainer_id = trainer_id

    exe = fluid.Executor(place)
    exe.run(startup_prog)

    if args.init_checkpoint and args.init_checkpoint != "":
        init_checkpoint(exe, args.init_checkpoint, train_program, args.use_fp16)
   
    data_reader = DataReader(
        data_dir=args.data_dir,
        batch_size=args.batch_size,
        in_tokens=args.in_tokens,
        vocab_path=args.vocab_path,
        voc_size=bert_config['vocab_size'],
        epoch=args.epoch,
        shuffle_files=False,
        max_seq_len=args.max_seq_len,
        generate_neg_sample=args.generate_neg_sample)

    exec_strategy = fluid.ExecutionStrategy()
    exec_strategy.use_experimental_executor = args.use_fast_executor
    exec_strategy.num_threads = dev_count
    exec_strategy.num_iteration_per_drop_scope = args.num_iteration_per_drop_scope

    build_strategy = fluid.BuildStrategy()
    build_strategy.num_trainers = nccl2_num_trainers
    build_strategy.trainer_id = nccl2_trainer_id

    # use_ngraph is for CPU only, please refer to README_ngraph.md for details
    # use_ngraph = os.getenv('FLAGS_use_ngraph')
    use_ngraph = True
    if not use_ngraph:
        train_compiled_program = fluid.CompiledProgram(train_program).with_data_parallel(
                 loss_name=total_loss.name,
                 exec_strategy=exec_strategy,
                 build_strategy=build_strategy)

    if args.validation_set_dir and args.validation_set_dir != "":
        predict = predict_wrapper(
            args,
            exe,
            bert_config,
            test_prog=test_prog,
            pyreader=test_pyreader,
            fetch_list=[
                next_sent_acc.name, mask_lm_loss.name, total_loss.name
            ])

    #i = 0
    #for d in data_reader.data_generator()():
    #    print(d)
    #    i += 1
    #    if i == 10: break
    #exit()

    train_pyreader.decorate_batch_generator(data_reader.data_generator())
    train_pyreader.start()
    steps = 0
    cost = []
    lm_cost = []
    acc = []
    time_begin = time.time()
    while steps < args.num_train_steps:
        try:
            steps += nccl2_num_trainers
            skip_steps = args.skip_steps * nccl2_num_trainers

            if nccl2_trainer_id != 0:
                if use_ngraph:
                    exe.run(fetch_list=[], program=train_program, use_program_cache=True)
                else:
                    exe.run(fetch_list=[], program=train_compiled_program)
                continue

            if steps % skip_steps != 0:
                if use_ngraph:
                    exe.run(fetch_list=[], program=train_program, use_program_cache=True)
                else:
                    exe.run(fetch_list=[], program=train_compiled_program)

            else:
                if use_ngraph:
                    each_next_acc, each_mask_lm_cost, each_total_cost, np_lr = exe.run(
                        fetch_list=[
                            next_sent_acc.name, mask_lm_loss.name, total_loss.name,
                            scheduled_lr.name], program=train_program, use_program_cache=True)
                else:
                    each_next_acc, each_mask_lm_cost, each_total_cost, np_lr = exe.run(
                        fetch_list=[
                            next_sent_acc.name, mask_lm_loss.name, total_loss.name,
                            scheduled_lr.name], program=train_compiled_program)

                acc.extend(each_next_acc)
                lm_cost.extend(each_mask_lm_cost)
                cost.extend(each_total_cost)

                print("feed_queue size", train_pyreader.queue.size())
                time_end = time.time()
                used_time = time_end - time_begin
                epoch, current_file_index, total_file, current_file = data_reader.get_progress(
                )
                print("current learning_rate:%f" % np_lr[0])
                print("epoch: %d, progress: %d/%d, step: %d, loss: %f, "
                      "ppl: %f, next_sent_acc: %f, speed: %f steps/s, file: %s"
                      % (epoch, current_file_index, total_file, steps,
                         np.mean(np.array(cost)),
                         np.mean(np.exp(np.array(lm_cost))),
                         np.mean(np.array(acc)), skip_steps / used_time,
                         current_file))
                cost = []
                lm_cost = []
                acc = []
                time_begin = time.time()

            if steps % args.save_steps == 0:
                save_path = os.path.join(args.checkpoints, "step_" + str(steps))
                fluid.io.save_persistables(exe, save_path, train_program)

            if args.validation_set_dir and steps % args.validation_steps == 0:
                vali_cost, vali_lm_cost, vali_acc, vali_steps, vali_speed = predict(
                )
                print("[validation_set] epoch: %d, step: %d, "
                      "loss: %f, global ppl: %f, batch-averged ppl: %f, "
                      "next_sent_acc: %f, speed: %f steps/s" %
                      (epoch, steps,
                       np.mean(np.array(vali_cost) / vali_steps),
                       np.exp(np.mean(np.array(vali_lm_cost) / vali_steps)),
                       np.mean(np.exp(np.array(vali_lm_cost) / vali_steps)),
                       np.mean(np.array(vali_acc) / vali_steps), vali_speed))

        except fluid.core.EOFException:
            train_pyreader.reset()
            break

if __name__ == '__main__':
    print_arguments(args)
    check_cuda(args.use_cuda)
    if args.do_test:
        test(args)
    else:
        train(args)
