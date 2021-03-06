# --------------------------------------------------------------------------------------------------------------------
#
# Project:      MS lesion segmentation (master thesis)
#
# Description:  Script with the baseline model (UNet) for the segmentation of MS lesions in INSPIRATION dataset (cross-sectional version)
#
# Author:       Sergio Tascon Morales (Research intern at mediri GmbH, student of Master in Medical Imaging and Applications - MAIA)
#
# Details:      None
#
# --------------------------------------------------------------------------------------------------------------------

import os
import random
import torch
import nibabel as nib
import numpy as np  
import pandas as pd
from ms_segmentation.general.general import create_folder, list_folders, get_experiment_name, create_log, cls, save_image
from os.path import join as jp
from ms_segmentation.plot.plot import shim_slice, shim_overlay_slice, shim, shim_overlay, plot_learning_curve
from medpy.io import load
from ms_segmentation.data_generation.slice_manager import SlicesGroupLoaderTimeLoadAll, SlicesLoader, get_inference_slices, get_inference_slices_time, get_probs, undo_crop_images, RandomHorizontalFlipSlice, RandomRotationSlice, ToTensorSlice
from ms_segmentation.data_generation.patch_manager_3d import PatchLoader3DLoadAll, build_image, get_inference_patches, reconstruct_image
from ms_segmentation.architectures.unet_c_gru import UNet_ConvGRU_2D_alt, UNet_ConvLSTM_2D_alt, UNet_ConvLSTM_Goku
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.optim import Adadelta, Adam
import torchvision.transforms as transforms
from ms_segmentation.general.training_helper import EarlyStopping, exp_lr_scheduler, dice_loss_2d, create_training_validation_sets, tversky_loss2D
from sklearn.metrics import jaccard_score as jsc
from ms_segmentation.evaluation.metrics import compute_metrics


path_data = r'D:\dev\ms_data\Challenges\ISBI2015\ISBI_L'
#path_data = r'D:\dev\INS1'
path_train = jp(path_data, "train")
path_test = jp(path_data, "test")
#path_test = r'D:\dev\ISBI_CS\test'
#path_test = r'D:\dev\INS_Test\test'
debug = False
if(debug):
    experiment_name = "dummy_experiment_2DUNet_ConvLSTM_"
else: 
    experiment_name, curr_date, curr_time = get_experiment_name(the_prefix = "unet2dconvLSTM")
#experiment_name = 'unet3d_2020-03-24_16_08_04[ISBI1-17]'
path_results = jp(path_data, experiment_name)
create_folder(path_results)
path_models = jp(path_results, "models")
create_folder(path_models)
path_segmentations = jp(path_results, "results")
create_folder(path_segmentations)


# Visualize one subject
#img, img_header = load(jp(path_train, '00001', 'FLAIR_masked.nii.gz'))
#mask, mask_header = load(jp(path_train, '00001', 'mask_FLAIR.nii.gz'))
#shim_overlay_slice(img, mask, 25, alpha=0.4)
#shim(img, 16)



# Define options for training
options = {}
# training data path
options['training_path'] = path_train
# testing data path 
options['test_path'] = path_test
# train/validation split percentage
options['val_split']  = 0.2
# Define modalities to be considered
options['input_data'] = ['flair', 'mprage', 'pd'] #No format
# Define ground truth name
#options['gt'] = 'mask_FLAIR.nii.gz'
options['gt'] = 'mask1'
# Name of brain mask images
options['brain_mask'] = 'brain_mask'
options['num_classes'] = 2
options['normalize'] = True 
options['norm_type'] = 'zero_one'
options['batch_size'] = 10
options['patience'] =  20 #Patience for the early stopping
options['gpu_use'] = True
options['num_epochs'] = 200
options['optimizer'] = 'adam'
# Patch sampling type
options['patch_sampling'] = 'mask' # (mask, balanced or balanced+roi or non-uniform)
# Loss
options['loss'] = 'dice' # (dice, cross-entropy, tversky)
options['resample_each_epoch'] = False
options['num_timepoints'] = 3


# Organize the data in a dictionary 
input_dictionary = create_training_validation_sets(options, dataset_mode='l')


#Show one subject with brain mask
#case_to_show = list(input_dictionary['input_val_data'].keys())[0] #First case of validation set
#shim_overlay(nib.load(input_dictionary['input_val_data'][case_to_show][0]).get_fdata(), nib.load(input_dictionary['input_val_rois'][case_to_show][0]).get_fdata(), 16, alpha=0.5)

# Create training, validation and test patches

transf = transforms.Compose([   RandomHorizontalFlipSlice(),
                                RandomRotationSlice(5),
                                ToTensorSlice()])




print('Training data: ')
training_dataset = SlicesGroupLoaderTimeLoadAll(input_data=input_dictionary['input_train_data'],
                                       labels=input_dictionary['input_train_labels'],
                                       roi=input_dictionary['input_train_rois'],
                                       num_timepoints=options['num_timepoints'],
                                       normalize=options['normalize'],
                                       norm_type=options['norm_type'],
                                       out_size = (160,200),
                                       transform = transf)


hola = training_dataset.__getitem__(100)



training_dataloader = DataLoader(training_dataset, 
                                 batch_size=options['batch_size'],
                                 shuffle=True)

print('Validation data: ')
validation_dataset = SlicesGroupLoaderTimeLoadAll(input_data=input_dictionary['input_val_data'],
                                        labels=input_dictionary['input_val_labels'],
                                        roi=input_dictionary['input_val_rois'],
                                        num_timepoints=options['num_timepoints'],
                                        normalize=options['normalize'],
                                        norm_type=options['norm_type'],
                                        out_size = (160,200),
                                        transform = transf)

validation_dataloader = DataLoader(validation_dataset, 
                                   batch_size=options['batch_size'],
                                   shuffle=True)





#Get frequency of each label
if(options['loss'] == 'cross-entropy'):
    print("Computing frequency of positive and negative voxels in patches for weighted crossentropy...")
    num0 = 0
    num1 = 0
    for i in range(len(training_dataset)):
        batchsito = training_dataset.__getitem__(i)[1]
        num0 += len(batchsito[batchsito==0])
        num1 += len(batchsito[batchsito==1])


    for i in range(len(validation_dataset)):
        batchsito = validation_dataset.__getitem__(i)[1]
        num0 += len(batchsito[batchsito==0])
        num1 += len(batchsito[batchsito==1])


    k = 1/(1/num0 + 1/num1)
    weights = [k/num0, k/num1]
    print("Weights for cross-entropy: ", weights)

    class_weights = torch.FloatTensor(weights).cuda()


from torch.optim import Adadelta, Adam

# Define the model 
#lesion_model = UNet_ConvGRU_2D_alt(n_channels=len(options['input_data']), n_classes = options['num_classes'], bilinear = False)
#lesion_model = UNet_ConvLSTM_Vegeta(n_channels=len(options['input_data']), n_classes = options['num_classes'], bilinear = False)
#lesion_model = UNet_ConvLSTM_Goku(n_channels=len(options['input_data']), n_classes = options['num_classes'], bilinear = False)
lesion_model = UNet_ConvLSTM_2D_alt(n_channels=len(options['input_data']), n_classes = options['num_classes'], bilinear = False)

#lesion_model = UNet2D(n_channels = len(options['input_data']), n_classes = options['num_classes'], bilinear = False)

#lesion_model.cuda()
#input_tensor = torch.rand(5, 3, 3, 160, 200).cuda()
#pred = lesion_model(input_tensor)

options['model_name'] = lesion_model.__class__.__name__
model_name = 'ms_lesion_segmentation'


# define the torch.device
device = torch.device('cuda') if options['gpu_use'] else torch.device('cpu')

# define the optimizer
if options['optimizer'] == "adam":
    optimizer = Adam(lesion_model.parameters())
elif options['optimizer'] == "adadelta":
    optimizer = Adadelta(lesion_model.parameters())

# send the model to the device
lesion_model = lesion_model.to(device)

early_stopping = EarlyStopping(patience=options['patience'], verbose=True)

train_losses = []
val_losses = []
train_jaccs = []
val_jaccs = []

# training loop
training = True
train_complete = False
epoch = 1

if not training and options['loss'] == "cross-entropy":
    weights = [1.0, 40.0]
    class_weights = torch.FloatTensor(weights).cuda()

try:
    while training:
        cls()
        # epoch specific metrics
        train_loss = 0
        val_loss = 0
        jaccs_train = []
        jaccs_val = []
        
        # -----------------------------
        # training samples
        # -----------------------------
        
        # set the model into train mode
        lesion_model.train() #Put in train mode
        for b_t, (data, target) in enumerate(training_dataloader):
                print("Training. Mini-batch ", b_t+1, "/", len(training_dataloader))
                # process batches: each batch is composed by training (x) and labels (y)
                # x = [batch_size, num_modalities, patch_dim1, patch_dim2, patch_dim3]
                # y = [batch_size, 1, patch_dim1, patch_dim2, patch_dim3]
    
                x = data.type('torch.cuda.FloatTensor').to(device)
                y = target.type('torch.cuda.FloatTensor').to(device)
                
                # clear gradients
                optimizer.zero_grad() #Set gradients to zero for every new batch so that no accummulation takes place
                
                # infer the current batch 
                pred = lesion_model(x)
                
                # pred = [batch_size, num_classes, patch_dim1, patch_dim2, patch_dim3]

                # compute the loss. 
                # we ignore the index=2

                if options['loss']=='dice':
                    y_one_hot = pred.data.clone()
                    y_one_hot[...] = 0
                    y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)
                    loss = dice_loss_2d(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot)

                elif options['loss'] == 'tversky':
                    y_one_hot = pred.data.clone()
                    y_one_hot[...] = 0
                    y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)
                    loss = tversky_loss2D(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot, 0.4, 0.6)

                elif options['loss'] == 'cross-entropy':
                    loss = F.cross_entropy(torch.log(torch.clamp(pred, 1E-7, 1.0)),
                                        y.squeeze(dim=1).long(), weight=class_weights)
                else:
                    raise ValueError("Unknown loss")
                
                train_loss += loss.item()
                
                # backward loss and next step
                loss.backward()
                optimizer.step()

                # compute the accuracy
                # compute the accuracy
                lbl = y.cpu().numpy().reshape(-1)
                pred_labels = pred.detach().cpu().numpy().astype('float32')
                pred_labels = np.argmax(pred_labels, axis=1).reshape(-1).astype(np.uint8)
                batch_jacc = jsc(pred_labels,lbl, average = 'binary')
                print("Loss: ", loss.item())
                print("Training - Batch JSC: ", batch_jacc)
                print("Num 1s: ", len(pred_labels[pred_labels>0]))
                jaccs_train.append(batch_jacc)
                
                
        # -----------------------------
        # validation samples
        # -----------------------------
    
        # set the model into train mode
        lesion_model.eval() #Put in evaluation mode. Eg so that things like dropout don't occur during evaluation
        for b_v, (data, target) in enumerate(validation_dataloader):
                
                print("Validation. Mini-batch ", b_v+1, "/", len(validation_dataloader))
                x = data.type('torch.cuda.FloatTensor').to(device)
                y = target.type('torch.cuda.FloatTensor').to(device)
                
                # infer the current batch 
                with torch.no_grad(): #Don't consider the gradients
                    pred = lesion_model(x)
                
                    # compute the loss. 
                    # we ignore the index=2

                    # compute the loss. 
                    if options['loss']=='dice':
                        y_one_hot = pred.data.clone()
                        y_one_hot[...] = 0
                        y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)
                        loss = dice_loss_2d(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot)

                    elif options['loss'] == 'tversky':
                        y_one_hot = pred.data.clone()
                        y_one_hot[...] = 0
                        y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)
                        loss = tversky_loss2D(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot, 0.3, 0.7)


                    elif options['loss'] == 'cross-entropy':
                        loss = F.cross_entropy(torch.log(torch.clamp(pred, 1E-7, 1.0)),
                                        y.squeeze(dim=1).long(), weight=class_weights)
                    else:
                        raise ValueError("Unknown loss")
                    
                    
                    val_loss += loss.item()
                
                    lbl = y.cpu().numpy().reshape(-1)
                    pred_labels = pred.detach().cpu().numpy().astype('float32')
                    pred_labels = np.argmax(pred_labels, axis=1).reshape(-1).astype(np.uint8)
                    batch_jacc = jsc(pred_labels,lbl, average = 'binary')
                    print("Validation - Batch JSC: ", batch_jacc)
                    jaccs_val.append(batch_jacc)
        
        # compute mean metrics
        train_loss /= (b_t + 1)
        val_loss /= (b_v + 1)
        train_jacc = np.mean(np.array(jaccs_train))
        val_jacc = np.mean(np.array(jaccs_val))

        train_losses.append(train_loss)
        val_losses.append(val_loss)
        train_jaccs.append(train_jacc)
        val_jaccs.append(val_jacc)

        print('Epoch {:d} train_loss {:.4f} train_jacc {:.4f} val_loss {:.4f} val_jacc {:.4f}'.format(
            epoch, 
            train_loss, 
            train_jacc,
            val_loss,
            val_jacc))
        
        
        #Check conditions for early stopping. Save model if improvement in validation loss with respect to previous epoch
        early_stopping(val_loss, lesion_model, path_models)
        if early_stopping.early_stop:
          print("Early stopping")
          train_complete = True
          break
        
        # update epochs
        epoch += 1

        if epoch > options['num_epochs']:
            train_complete = True
            training = False
            

        # Load latest best model
    lesion_model.load_state_dict(torch.load(jp(path_models, "checkpoint.pt")))
except KeyboardInterrupt:
    # If training is stopped, load last model
    print("Training was stopped, loading last model...")
    lesion_model.load_state_dict(torch.load(jp(path_models, "checkpoint.pt")))

    plot_learning_curve(train_losses, val_losses, the_title="Learning curve", measure = "Loss (" + options["loss"] + ")", early_stopping = True, filename = jp(path_results, "loss_plot.png"))
    plot_learning_curve(train_jaccs, val_jaccs, the_title="Jaccard plot", measure = "Jaccard", early_stopping = False, filename = jp(path_results, "jaccard_plot.png"))                  

options['max_epoch_reached'] = epoch

                     
#plot_learning_curve(train_losses, val_losses, the_title="Learning curve", measure = "Loss (" + options["loss"] + ")", early_stopping = True, filename = jp(path_results, "loss_plot.png"))
#plot_learning_curve(train_jaccs, val_jaccs, the_title="Jaccard plot", measure = "Jaccard", early_stopping = False, filename = jp(path_results, "jaccard_plot.png"))

#Plot learning curve
if train_complete:
    plot_learning_curve(train_losses, val_losses, the_title="Learning curve", measure = "Loss (" + options["loss"] + ")", early_stopping = True, filename = jp(path_results, "loss_plot.png"))
    plot_learning_curve(train_jaccs, val_jaccs, the_title="Jaccard plot", measure = "Jaccard", early_stopping = False, filename = jp(path_results, "jaccard_plot.png"))
else:
    try:
        lesion_model.load_state_dict(torch.load(jp(path_models,"checkpoint.pt")))
    except:
        raise ValueError("No model found")

def divide_inference_slices(all_slices, desired_timepoints):
    #Format for all_slices is (num_slices, num_timepoints, num_modalities, Height, Width)
    _, num_timepoints, _, _, _ = all_slices.shape
    
    if(num_timepoints == desired_timepoints): #Special case if number of timepoints coincides with desired number of timepoints
        #TODO: special case -> How to predict timepoint in the middle
        pass

    list_inference_slices = []
    indexes = []
    num_forward = num_timepoints - desired_timepoints + 1
    for i in range(num_forward):
        list_inference_slices.append(all_slices[:,i:i+desired_timepoints,:,:,:])
        indexes.append(i+desired_timepoints)

    all_slices = np.flip(all_slices, axis = 1).copy()
    num_backward = num_forward
    if(num_timepoints%2 == 0): #even
        init = 0
    else: #odd
        init = 1
    for i in range(init,num_backward):
        list_inference_slices.append(all_slices[:,i:i+desired_timepoints,:,:,:])
        if(num_timepoints%2==0): #even
            indexes.append(i+1)
        else:
            indexes.append(i)
    
    sorted_indexes = np.argsort(indexes)
    sorted_list_inference_slices = [list_inference_slices[k] for k in sorted_indexes]

    return sorted_list_inference_slices


#Evaluate all images
columns = compute_metrics(None, None, labels_only=True)
df = pd.DataFrame(columns = columns)

test_images = list_folders(path_test)
i_row=0
cnt=0
for case in test_images:
    print(cnt+1, "/", len(test_images))
    scan_path = jp(path_test, case)
    inf_slices = get_inference_slices_time( the_path=path_test,
                                            the_case=case,
                                            input_data=options['input_data'], 
                                            normalize=True, 
                                            out_size = (160,200),
                                            norm_type = 'zero_one')
    
    inf_slices_sets = divide_inference_slices(inf_slices, options['num_timepoints'])
    #evaluate each of the timepoints

    for i_timepoint in range(len(inf_slices_sets)):

        inf_slices = inf_slices_sets[i_timepoint] #Take first three time points (to predict third one)

        probs = get_probs(inf_slices, lesion_model, device, options['num_classes'], options)
        labels = np.transpose(np.argmax(probs, axis=1).astype(np.uint8), (1,2,0))

        #Read ground truth
        labels_gt = nib.load(jp(path_test, case, options['gt']+ "_" + str(i_timepoint+1).zfill(2) +".nii.gz")).get_fdata().astype(np.uint8)  #GT  

        full_image = np.zeros_like(labels_gt, dtype=np.uint8)
        labels = undo_crop_images(full_image, labels, (160,200))

        #DSC
        metrics = compute_metrics(labels_gt, labels)

        df.loc[i_row] = list(metrics.values())
        i_row += 1
        #Save result
        img_nib = nib.Nifti1Image(labels, np.eye(4))
        create_folder(jp(path_segmentations, case))
        nib.save(img_nib, jp(path_segmentations, case, case+"_"+ str(i_timepoint+1).zfill(2) +"_segm.nii.gz"))

        cnt += 1

df.loc[i_row] = list(df.mean()) # Last row is average of previous rows
df.to_csv(jp(path_results, "results.csv"), float_format = '%.5f', index = False)
print(df.mean())
create_log(path_results, options)




