"""
main entry of the script, train, validate and test
"""
import torch
import argparse
import random

from others.logging import logger, init_logger
from models.ps_model import ProductRanker, build_optim
from data.data_util import GlobalProdSearchData, ProdSearchData
from trainer import Trainer

def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', default=666, type=int)
    parser.add_argument("--train_from", default='')
    parser.add_argument("--test_from", default='')
    parser.add_argument("--dropout", default=0.1, type=float)
    parser.add_argument("--optim", type=str, default="adam", help="sgd or adam")
    parser.add_argument("--lr", default=0.002, type=float) #0.002
    parser.add_argument("--beta1", default= 0.9, type=float)
    parser.add_argument("--beta2", default=0.999, type=float)
    parser.add_argument("--decay_method", default='noam', type=str) #warmup learning rate then decay
    parser.add_argument("--warmup_steps", default=3000, type=int) #10000
    parser.add_argument("--max_grad_norm", type=float, default=5.0,
                            help="Clip gradients to this norm.")
    parser.add_argument("--subsampling_rate", type=float, default=1e-5,
                            help="The rate to subsampling.")
    parser.add_argument("--L2_lambda", type=float, default=0.0,
                            help="The lambda for L2 regularization.")
    parser.add_argument("--batch_size", type=int, default=32,
                            help="Batch size to use during training.")
    parser.add_argument("--valid_batch_size", type=int, default=16,
                            help="Batch size for validation to use during training.")
    parser.add_argument("--candi_batch_size", type=int, default=1000,
                            help="Batch size for validation to use during training.")
    parser.add_argument("--num_workers", type=int, default=4,
                            help="Number of processes to load batches of data during training.")
    parser.add_argument("--data_dir", type=str, default="/tmp", help="Data directory")
    parser.add_argument("--input_train_dir", type=str, default="", help="The directory of training and testing data")
    parser.add_argument("--save_dir", type=str, default="/tmp", help="Model directory & output directory")
    parser.add_argument("--log_file", type=str, default="train.log", help="log file name")
    parser.add_argument("--query_encoder_name", type=str, default="fs", choices=["fs","avg"],
            help="Specify network structure parameters. Please read readme.txt for details.")
    parser.add_argument("--review_encoder_name", type=str, default="pvc", choices=["pv", "pvc", "fs", "avg"],
            help="Specify network structure parameters. ")
    parser.add_argument("--embedding_size", type=int, default=128, help="Size of each embedding.")
    parser.add_argument("--ff_size", type=int, default=512, help="size of feedforward layers in transformers.")
    parser.add_argument("-heads", default=8, type=int, help="attention heads in transformers")
    parser.add_argument("-inter_layers", default=2, type=int, help="transformer layers")
    parser.add_argument("--review_word_limit", type=int, default=100,
                            help="the limit of number of words in reviews.")
    parser.add_argument("--uprev_review_limit", type=int, default=10,
                            help="the number of users previous reviews used.")
    parser.add_argument("--iprev_review_limit", type=int, default=30,
                            help="the number of item's previous reviews used.")
    parser.add_argument("--pv_window_size", type=int, default=5, help="Size of context window.")
    parser.add_argument("--corrupt_rate", type=float, default=0.9, help="the corruption rate that is used to represent the paragraph in the corruption module.")
    parser.add_argument("--max_pvc_word_count", type=int, default=50, help="number of words that represent the paragraph in the corruption module.")
    parser.add_argument("--shuffle_review_words", type=str2bool, nargs='?',const=True,default=True,help="shuffle review words before collecting sliding words.")
    parser.add_argument("--train_review_only", type=str2bool, nargs='?',const=True,default=True,help="whether the representation of negative products need to be learned at each step.")
    parser.add_argument("--max_train_epoch", type=int, default=5,
                            help="Limit on the epochs of training (0: no limit).")
    parser.add_argument("--start_epoch", type=int, default=0,
                            help="the epoch where we start training.")
    parser.add_argument("--steps_per_checkpoint", type=int, default=200,
                            help="How many training steps to do per checkpoint.")
    parser.add_argument("--neg_per_pos", type=int, default=5,
                            help="How many negative samples used to pair with postive results.")
    parser.add_argument("--sparse_emb", action='store_true',
                            help="use sparse embedding or not.")
    parser.add_argument("--scale_grad", action='store_true',
                            help="scale the grad of word and av embeddings.")
    parser.add_argument("-nw", "--weight_distort", action='store_true',
                            help="Set to True to use 0.75 power to redistribute for neg sampling .")
    parser.add_argument("--decode", action='store_true',
                            help="Set to True for testing.")
    parser.add_argument("--test_mode", type=str, default="product_scores",
            help="Test modes: product_scores -> output ranking results and ranking scores; (default is product_scores)")
    parser.add_argument("--rank_cutoff", type=int, default=100,
                            help="Rank cutoff for output ranklists.")
    parser.add_argument('--device', default='cuda', choices=['cpu', 'cuda'], help="use CUDA or cpu")
    return parser.parse_args()

model_flags = ['embedding_size', 'ff_size', 'heads', 'inter_layers','review_encoder_name','query_encoder_name']

def create_model(args, prod_data, load_path=''):
    """Create translation model and initialize or load parameters in session."""
    model = ProductRanker(args, args.device, prod_data.vocab_size,
            prod_data.review_count, prod_data.word_dists)
    if load_path != '':
        logger.info('Loading checkpoint from %s' % load_path)
        checkpoint = torch.load(load_path,
                                map_location=lambda storage, loc: storage)
        opt = vars(checkpoint['opt'])
        for k in opt.keys():
            if (k in model_flags):
                setattr(args, k, opt[k])
        args.start_epoch = checkpoint['epoch']
        model.load_cp(checkpoint)
        optim = build_optim(args, model, checkpoint)
    else:
        optim = build_optim(args, model, None)
    logger.info(model)
    return model, optim

def train(args):
    args.start_epoch = 0
    init_logger(args.log_file)
    logger.info('Device %s' % args.device)
    if args.device == "cuda":
        torch.cuda.manual_seed(args.seed)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    global_data = GlobalProdSearchData(args, args.data_dir, args.input_train_dir)
    train_prod_data = ProdSearchData(args, args.input_train_dir, "train",
            global_data.vocab_size, global_data.review_count,
            global_data.user_size, global_data.product_size)
    #subsampling has been done in train_prod_data
    model, optim = create_model(args, train_prod_data, args.train_from)
    trainer = Trainer(args, model, optim)
    valid_prod_data = ProdSearchData(args, args.input_train_dir, "valid",
            global_data.vocab_size, global_data.review_count,
            global_data.user_size, global_data.product_size)
    best_checkpoint_path = trainer.train(trainer.args, global_data, train_prod_data, valid_prod_data)
    test_prod_data = ProdSearchData(args, args.input_train_dir, "test",
            global_data.vocab_size, global_data.review_count,
            global_data.user_size, global_data.product_size)
    best_model, _ = create_model(args, train_prod_data, best_checkpoint_path)
    del trainer
    torch.cuda.empty_cache()
    trainer = Trainer(args, best_model, None)
    trainer.test(args, global_data, test_prod_data)


def get_product_scores(args):
    init_logger(args.log_file)
    global_data = GlobalProdSearchData(args, args.data_dir, args.input_train_dir)
    test_prod_data = ProdSearchData(args, args.input_train_dir, "test",
            global_data.vocab_size, global_data.review_count,
            global_data.user_size, global_data.product_size)
    best_model, _ = create_model(args, test_prod_data, args.test_from)
    trainer = Trainer(args, best_model, None)
    trainer.test(args, global_data, test_prod_data)

def main(args):
    if args.decode:
            get_product_scores(args)
    else:
        train(args)
if __name__ == '__main__':
    main(parse_args())