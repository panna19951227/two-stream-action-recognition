"""
LSTM model for action recognition
Author: Lili Meng menglili@cs.ubc.ca, March 12th, 2018
"""

from __future__ import print_function
import sys
import os
import math
import shutil
import random
import tempfile
import unittest
import traceback
import torch
import torch.utils.data
import torch.cuda
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.serialization import load_lua
from tensorboardX import SummaryWriter
import torchvision.transforms as transforms

import argparse

import numpy as np
import time
from PIL import Image
from network import *
from feature_dataloader import *
use_cuda = True

class Action_Att_LSTM(nn.Module):
	def __init__(self, input_size, hidden_size, output_size, seq_len):
		super(Action_Att_LSTM, self).__init__()
		#attention
		self.att_vw = nn.Linear(49*2048, 49, bias=False)
		self.att_hw = nn.Linear(hidden_size, 49, bias=False)
		self.att_bias = nn.Parameter(torch.zeros(49))
		#self.att_w = nn.Linear(49, 1, bias=False)
		self.att_vw_bn= nn.BatchNorm1d(49)
		self.att_hw_bn= nn.BatchNorm1d(49)
		self.hidden_size = hidden_size
		self.lstm = nn.LSTM(input_size, hidden_size)
		self.fc = nn.Linear(hidden_size, output_size)
		self.fc_attention = nn.Linear(hidden_size, seq_len)
		self.fc_out = nn.Linear(hidden_size, output_size)
		self.fc_c0_0 = nn.Linear(2048, 1024)
		self.fc_c0_1 = nn.Linear(1024, 512)
		self.fc_h0_0 = nn.Linear(2048, 1024)
		self.fc_h0_1 = nn.Linear(1024, 512)
		self.input_size = input_size

		self.lstm_cell = nn.LSTMCell(input_size, hidden_size)
		
	def _attention_layer(self, features, hiddens, batch_size):
	  	"""
	  	: param features: (batch_size, 49 *2048)
	  	: param hiddens: (batch_size, hidden_dim)
	  	:return:
	  	"""
	  	features_tmp = features.contiguous().view(batch_size, -1)
	  	
	  	#att_fea = self.att_vw(features_tmp)
	  	#att_fea = self.att_vw_bn(att_fea)
	  	att_h = self.att_hw(hiddens)
	  	#att_h = self.att_hw_bn(att_h)
	  	#att_out = att_fea + att_h 
	  	att_out = att_h

	  	#print("torch.norm(att_fea): ", torch.norm(att_fea))
	  	print("torch.norm(att_h): ", torch.norm(att_h))


	  	alpha = nn.Softmax()(att_out)

	  	context = torch.sum(features * alpha.unsqueeze(2), 1)
	  	
	  	return context, alpha
	
	def get_start_states(self, input_x):

		h0 = torch.mean(torch.mean(input_x,2),2)
		h0 = self.fc_h0_0(h0)
		h0 = self.fc_h0_1(h0)

		c0 = torch.mean(torch.mean(input_x,2),2)
		c0 = self.fc_c0_0(c0)
		c0 = self.fc_c0_1(c0)
		
		return h0, c0
	
	def forward(self, input_x):

		batch_size = input_x.shape[0]
		seq_len = input_x.shape[2]
		h0, c0 = self.get_start_states(input_x)

		output_list = []
		alpha_list = []
		for step in range(seq_len):
			
			tmp = input_x[:,:,step,:].transpose(1,2)
			feas, alpha = self._attention_layer(tmp, h0, batch_size)
			h0, c0 = self.lstm_cell(feas, (h0, c0))
			output = self.fc_out(h0) 
			output_list.append(output)
			alpha_list.append(alpha)
			
		
		final_output =  torch.mean(torch.stack(output_list, dim=0),0)
		
		alpha_total = torch.stack(alpha_list).transpose(0,1)

		return final_output, alpha_total

	def init_hidden(self, batch_size):
		result = Variable(torch.zeros(1, batch_size, self.hidden_size))
		if use_cuda:
			return result.cuda()
		else:
			return result


def lr_scheduler(optimizer, epoch_num, init_lr = 0.001, lr_decay_epochs=10):
	"""Decay learning rate by a factor of 0.1 every lr_decay_epochs.
	"""
	using_cyclic_lr = False
	if using_cyclic_lr == True:
		eta_min = 5e-8
		eta_max = 5e-5

		lr =  eta_min + 0.5 * (eta_max - eta_min) * (1 + np.cos(epoch_num/FLAGS.max_epoch * np.pi))
	else:
		lr = init_lr *(0.1**(epoch_num//lr_decay_epochs))
		if epoch_num % lr_decay_epochs == 0:
			print("Learning rate changed to be : {}".format(lr))


	for param_group in optimizer.param_groups:
		param_group['lr'] = lr

	return optimizer


def train(batch_size,
		  train_data,
		  train_label,
		  model,
		  model_optimizer,
		  criterion):
	"""
	a training sample which goes through a single step of training.
	"""
	loss = 0
	model_optimizer.zero_grad()

	print("train_data.shape: ", train_data.shape)
	logits, spa_att_weights = model.forward(train_data)

	loss += criterion(logits, train_label) 

	loss.backward()

	model_optimizer.step()

	final_loss = loss.data[0]/batch_size

	corrects = (torch.max(logits, 1)[1].view(train_label.size()).data == train_label.data).sum()

	train_accuracy = 100.0 * corrects/batch_size

	return final_loss, train_accuracy, spa_att_weights, corrects

def test_step(batch_size,
			 batch_x,
			 batch_y,
			 model):
	
	print("test_data.shape: ", batch_x.shape)
	test_logits, spa_att_weights = model.forward(batch_x)
	
	corrects = (torch.max(test_logits, 1)[1].view(batch_y.size()).data == batch_y.data).sum()

	test_accuracy = 100.0 * corrects/batch_size

	return test_logits, test_accuracy, spa_att_weights, corrects


def main():

	torch.manual_seed(1234)
	dataset_name = FLAGS.dataset

	maxEpoch = FLAGS.max_epoch

	num_segments = FLAGS.num_segments

	# load train data
	train_data_dir = './spa_features/train'
	train_csv_file = './spa_features/train_features_list.csv'

	train_data_loader = get_loader(data_dir=train_data_dir, 
							csv_file = train_csv_file, 
							batch_size = FLAGS.train_batch_size,
							mode ='train',
							dataset='hmdb51')
	# load test data
	test_data_dir = './spa_features/test'
	test_csv_file = './spa_features/test_features_list.csv'
	test_data_loader = get_loader(data_dir = test_data_dir, 
    						csv_file = test_csv_file, 
    						batch_size = FLAGS.test_batch_size, 
    						mode='test',
                            dataset='hmdb51')

	category_dict = np.load("./category_dict.npy")

	lstm_action = Action_Att_LSTM(input_size=2048, hidden_size=512, output_size=51, seq_len=FLAGS.num_segments).cuda()
	#model_optimizer = torch.optim.Adam(lstm_action.parameters(), lr=1e-5) 
	model_optimizer = torch.optim.SGD(lstm_action.parameters(), lr=1e-3)
	criterion = nn.CrossEntropyLoss()  

	best_test_accuracy = 0
	
	log_dir = os.path.join('./51HMDB51_tensorboard', 'nobias_SGD1e-3_spa_att_hiddenOnly512_LSoftmaxonlyhidden'+time.strftime("_%b_%d_%H_%M", time.localtime()))

	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	writer = SummaryWriter(log_dir)

	num_step_per_epoch_train = 3570/FLAGS.train_batch_size
	num_step_per_epoch_test = 1530/FLAGS.test_batch_size
	for epoch_num in range(maxEpoch):

		model_optimizer = lr_scheduler(optimizer = model_optimizer, epoch_num=epoch_num, init_lr = 1e-3, lr_decay_epochs=70)
		
		lstm_action.train()
		avg_train_accuracy = 0

		train_name_list =[]
		train_spa_att_weights_list = []
		total_train_corrects = 0
		for i, (train_sample,train_batch_name) in enumerate(train_data_loader):
			train_batch_feature = train_sample['feature'].transpose(1,2)
			train_batch_label = train_sample['label']
			train_batch_feature = Variable(train_batch_feature).cuda().float()
			train_batch_label = Variable(train_batch_label[:,0]).cuda().long()
			
			train_loss, train_accuracy, train_spa_att_weights, train_corrects = train(FLAGS.train_batch_size, train_batch_feature, train_batch_label, lstm_action, model_optimizer, criterion)
			#print("train_spa_att_weights[0:5] ",train_spa_att_weights[0:5])
			train_name_list.append(train_batch_name)
			train_spa_att_weights_list.append(train_spa_att_weights)
			avg_train_accuracy+=train_accuracy
			print("batch {}, train_acc: {} ".format(i, train_accuracy))
			total_train_corrects+= train_corrects
			
		train_spa_att_weights_np = torch.cat(train_spa_att_weights_list, dim=0)
		avg_train_corrects = total_train_corrects *100 /3570
		#print("train_spa_att_weights_np.shape: ",train_spa_att_weights_np.shape)
		#np.save("./saved_weights/hc_train_name.npy", np.asarray(train_name_list))
		#np.save("./saved_weights/hc_train_att_weights.npy", train_spa_att_weights_np.cpu().data.numpy())
		final_train_accuracy = avg_train_accuracy/num_step_per_epoch_train
		print("epoch: "+str(epoch_num)+ " train accuracy: " + str(final_train_accuracy))
		print("epoch: "+str(epoch_num)+ " train corrects: " + str(avg_train_corrects))
		writer.add_scalar('train_accuracy', final_train_accuracy, epoch_num)
   

		save_train_file = "hid_current"+FLAGS.dataset  + "_numSegments"+str(FLAGS.num_segments)+"_regFactor_"+str(FLAGS.hp_reg_factor)+"_train_acc.txt"
		with open(save_train_file, "a") as text_file:
				print(f"{str(final_train_accuracy)}", file=text_file)

		avg_test_accuracy = 0
		lstm_action.eval()
		test_name_list =[]
		test_spa_att_weights_list = []
		total_test_corrects = 0
		for i, (test_sample, test_batch_name) in enumerate(test_data_loader):
			test_batch_feature = test_sample['feature'].transpose(1,2)
			test_batch_label = test_sample['label']
			
			test_batch_feature = Variable(test_batch_feature, volatile=True).cuda().float()
			test_batch_label = Variable(test_batch_label[:,0], volatile=True).cuda().long()
			

			test_logits, test_accuracy, test_spa_att_weights, test_corrects = test_step(FLAGS.test_batch_size, test_batch_feature, test_batch_label, lstm_action)

			test_name_list.append(test_batch_name)
			test_spa_att_weights_list.append(test_spa_att_weights)
			
			print("batch_test_accuracy: ", test_accuracy)
			total_test_corrects += test_corrects 

			avg_test_accuracy+= test_accuracy

		avg_test_corrects = total_test_corrects*100/1530

		test_spa_att_weights_np = torch.cat(test_spa_att_weights_list, dim=0)
		print("test_spa_att_weights_np.shape ", test_spa_att_weights_np.shape)
		#np.save("./saved_weights/hc_test_name.npy", np.asarray(test_name_list))
		#np.save("./saved_weights/hc_test_att_weights.npy", test_spa_att_weights_np.cpu().data.numpy())
	
		final_test_accuracy = avg_test_accuracy/num_step_per_epoch_test
		print("epoch: "+str(epoch_num)+ " test accuracy: " + str(final_test_accuracy))
		print("epoch: "+str(epoch_num)+ " test corrects: " + str(avg_test_corrects))
		writer.add_scalar('test_accuracy', final_test_accuracy, epoch_num)

		save_test_file = "hid_current"+FLAGS.dataset  + "_numSegments"+str(FLAGS.num_segments)+"_regFactor_"+str(FLAGS.hp_reg_factor)+"_test_acc.txt"
		with open(save_test_file, "a") as text_file1:
				print(f"{str(final_test_accuracy)}", file=text_file1)

		if final_test_accuracy > best_test_accuracy:
			best_test_accuracy = final_test_accuracy
		print('\033[91m' + "best test accuracy is: " +str(best_test_accuracy)+ '\033[0m') 
		
	# export scalar data to JSON for external processing
	#writer.export_scalars_to_json("./saved_logs/all_scalars.json")
	writer.close()
			
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='HMDB51',
                        help='dataset: "UCF101", "HMDB51"')
    parser.add_argument('--train_batch_size', type=int, default=64,
                    	help='train_batch_size: [64]')
    parser.add_argument('--test_batch_size', type=int, default=64,
                    	help='test_batch_size: [64]')
    parser.add_argument('--max_epoch', type=int, default=200,
                    	help='max number of training epoch: [60]')
    parser.add_argument('--num_segments', type=int, default=22,
                    	help='num of segments per video: [110]')
    parser.add_argument('--use_changed_lr', dest='use_changed_lr',
    					help='not use change learning rate by default', action='store_true')
    parser.add_argument('--use_regularizer', dest='use_regularizer',
    					help='use regularizer', action='store_false')
    parser.add_argument('--hp_reg_factor', type=float, default=0,
                        help='multiply factor for regularization. [0]')
    FLAGS, unparsed = parser.parse_known_args()
    if len(unparsed) > 0:
        raise Exception('Unknown arguments:' + ', '.join(unparsed))
    print(FLAGS)
main()