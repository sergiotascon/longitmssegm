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
from ms_segmentation.general.general import print_line, save_this, list_files_with_name_containing, create_folder, list_folders, save_image, get_experiment_name, create_log, cls, get_dictionary_with_paths
from os.path import join as jp
from ms_segmentation.plot.plot import shim_slice, shim_overlay_slice, shim, shim_overlay, plot_learning_curve
from medpy.io import load
from ms_segmentation.data_generation.patch_manager_3d import (PatchLoader3DTime, PatchLoader3DTimeLoadAll, PatchLoader3DLoadAll, \
                                                            build_image, get_inference_patches, reconstruct_image, RandomFlipX, RandomFlipY, RandomFlipZ, \
                                                                RandomRotationXY, RandomRotationXZ, RandomRotationYZ, ToTensor3DPatch)
from ms_segmentation.architectures.unet3d import UNet_3D_alt, UNet_3D_double_encoder#, UNet3D_1, UNet3D_2
from ms_segmentation.architectures.unet_c_gru import UNet_ConvGRU_3D_1, UNet_ConvLSTM_3D_alt, UNet_ConvLSTM_3D_encoder
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.optim import Adadelta, Adam
import torchvision.transforms as transforms
from ms_segmentation.general.training_helper import EarlyStopping, exp_lr_scheduler, dice_loss, create_training_validation_sets
from sklearn.metrics import jaccard_score as jsc
from ms_segmentation.evaluation.metrics import compute_metrics


debug = False 
if debug:
    print("Debugging mode ...")


options = {}
options['val_split']  = 0.2
options['input_data'] = ['flair_norm', 'mprage_norm', 'pd_norm', 't2_norm']
#options['input_data'] = ['flair', 'pd_times_fl', 't2_times_fl', 't1_inv_times_fl', 'sum_times_fl']
options['gt'] = 'mask1'
options['brain_mask'] = 'brain_mask'
options['num_classes'] = 2
options['patch_size'] = (32,32,32)
options['sampling_step'] = (16,16,16)
options['normalize'] = True 
options['norm_type'] = 'zero_one'
options['batch_size'] = 16
options['patience'] =  20 #Patience for the early stopping
options['gpu_use'] = True
options['num_epochs'] = 200
options['optimizer'] = 'adam'
options['num_timepoints'] = 3
options['patch_sampling'] = 'mask' # (mask, balanced or balanced+roi or non-uniform)
options['loss'] = 'dice' # (dice, cross-entropy)
options['resample_each_epoch'] = False


path_base = r'D:\dev\ms_data\Challenges\ISBI2015\ISBI_L'
path_data = jp(path_base, 'longitudinal_normalized_images')          # ACHTUUUNG
options['path_data'] = path_data
path_res = jp(path_base, "cross_validation")
all_patients = list_folders(path_data)



if(debug):
    experiment_name = "dummy_UNet_ConvLSTM3D"
else:
    experiment_name, curr_date, curr_time = get_experiment_name(the_prefix = "CROSS_VALIDATION_UNetConvLSTM3D")

validation_images = ['03', '03', '04', '05', '02'] # so that all experiments use the same validation image

#experiment_name = 'CROSS_VALIDATION_UNetConvLSTM3D_2020-06-13_08_43_21'
fold = 0
for curr_test_patient in all_patients:
    fold += 1

    #For cross-validation it is necessary to remove fields so that they do not cause errors in function create_training_validation_sets
    if "training_path" in options:
        del options['training_path']
    if "test_path" in options:
        del options['test_path']

    curr_train_patients = all_patients.copy()
    curr_train_patients.remove(curr_test_patient)

    options['training_samples'] = curr_train_patients
    options['test_samples'] = [curr_test_patient]

    experiment_folder = jp(path_res, experiment_name) 
    create_folder(experiment_folder)

    path_results = jp(experiment_folder, "fold" + str(fold).zfill(2))
    create_folder(path_results)
    path_models = jp(path_results, "models")
    create_folder(path_models)
    path_segmentations = jp(path_results, "results")
    create_folder(path_segmentations)


    # Organize the data in a dictionary 
    input_dictionary = create_training_validation_sets(options, dataset_mode="l", pad_repeat=True, specific_val=[validation_images[fold-1]])

    # Create training, validation and test patches

    rotation_angle=5
    options["transforms"] = None
    #options["transforms"] transforms.Compose([   RandomFlipX(),
                                #     RandomFlipY(),
                                #     RandomFlipZ(),
                                #     #RandomRotationXY(rotation_angle),
                                #     #RandomRotationYZ(rotation_angle),
                                #     #RandomRotationXZ(rotation_angle),
                                #     ToTensor3DPatch()
                                # ])

    
    print('Training data: ')
    training_dataset = PatchLoader3DTimeLoadAll(input_data=input_dictionary['input_train_data'],
                                        labels=input_dictionary['input_train_labels'],
                                        rois=input_dictionary['input_train_rois'],
                                        patch_size=options['patch_size'],
                                        sampling_step=options['sampling_step'],
                                        normalize=options['normalize'],
                                        norm_type=options['norm_type'],
                                        sampling_type=options['patch_sampling'],
                                        resample_epoch=options['resample_each_epoch'],
                                        num_timepoints = options['num_timepoints'])

    #hola = training_dataset.__getitem__(2200)

    training_dataloader = DataLoader(training_dataset, 
                                    batch_size=options['batch_size'],
                                    shuffle=True)

    print('Validation data: ')
    validation_dataset = PatchLoader3DTimeLoadAll(input_data=input_dictionary['input_val_data'],
                                            labels=input_dictionary['input_val_labels'],
                                            rois=input_dictionary['input_val_rois'],
                                            patch_size=options['patch_size'],
                                            sampling_step=options['sampling_step'],
                                            normalize=options['normalize'],
                                            norm_type=options['norm_type'],
                                            sampling_type=options['patch_sampling'],
                                            num_timepoints = options['num_timepoints'])

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



    # Define the Unet model 
    # 2 input channels (FLAIR and T1)
    # 2 output classes (healthy and MS lesion)
    #lesion_model = Unet3D(input_size=len(options['input_data']), output_size=options['num_classes'])
    #lesion_model = UNet_3D_double_encoder(n_channels_t=options['num_timepoints'], n_channels_m=len(options['input_data']), n_classes=options['num_classes'], bilinear=False)
    lesion_model = UNet_ConvLSTM_3D_encoder(n_channels=len(options['input_data']), n_classes=options['num_classes'], bilinear=False)
    #lesion_model = torch.cat([x3_1, x3_2], dim=1))(input_size=len(options['input_data']), output_size=2)
    #lesion_model = UNet3D_2(input_size=len(options['input_data']), output_size=2)
    #lesion_model.cuda()
    #input_tensor = torch.rand(16, 3, 4, 32, 32, 32).cuda()
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

    #To try:
    # pw = [num0/num1]
    # cw = torch.FloatTensor(pw).cuda()
    # criterion = torch.nn.BCEWithLogitsLoss(pos_weight = cw)

    # all_losses_train = []
    # all_losses_val = []
    # all_dices_train = []
    # all_dices_val = []

    def save_batch(x, y):
        x_np = x.detach().cpu().numpy() # 5,4,2,32,32,32
        y_np = y.detach().cpu().numpy() # 5,1,32,32,32
        for i_batch in range(x_np.shape[0]):
            save_image(y_np[i_batch, 0,:,:,:], "label_batch_"+ str(i_batch) +".nii.gz")
            for i_tp in range(x_np.shape[1]):
                for i_seq in range(x_np.shape[2]):
                    save_image(x_np[i_batch, i_tp, i_seq, :, :, :], "img_batch_"+str(i_batch)+ "_tp_"+str(i_tp)+"_seq_"+str(i_seq) + ".nii.gz")
    

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
                    #print("Training. Mini-batch ", b_t+1, "/", len(training_dataloader))
                    # process batches: each batch is composed by training (x) and labels (y)
                    # x = [batch_size, num_timepoints, num_modalities, patch_dim1, patch_dim2, patch_dim3]
                    # y = [batch_size, num_timepoints, 1, patch_dim1, patch_dim2, patch_dim3]
        
                    x = data.to(device)
                    y = target[:,0,:,:,:,:].to(device) #Take GT in the middle
                    
                    #save_batch(x, y)

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
                        #y_one_hot = torch.FloatTensor(x.size(0), 2, x.size(2), x.size(3))
                        #y_one_hot = y_one_hot.to(device)
                        y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)
                        #y_one_hot.scatter_(1,y.unsqueeze(1).type(torch.LongTensor).to(device),1)

                        loss = dice_loss(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot)
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
                    print("*** Fold:", fold, "  , Epoch:", epoch, " ***")
                    print("Training. Mini-batch ", b_t+1, "/", len(training_dataloader))
                    print("Training Loss: {:.4f}, Jacc: {:.4f}".format(loss.item(), batch_jacc))
                    print_line()
                    jaccs_train.append(batch_jacc)
                    
                    
            # -----------------------------
            # validation samples
            # -----------------------------
        
            # set the model into train mode
            lesion_model.eval() #Put in evaluation mode. Eg so that things like dropout don't occur during evaluation
            for b_v, (data, target) in enumerate(validation_dataloader):
                    
                    print("Validation. Mini-batch ", b_v+1, "/", len(validation_dataloader))
                    x = data.to(device)
                    y = target[:,0,:,:,:,:].to(device)
                    
                    # infer the current batch 
                    with torch.no_grad(): #Don't consider the gradients
                        pred = lesion_model(x)
                    
                        # compute the loss. 
                        # we ignore the index=2

                        # compute the loss. 
                        if options['loss']=='dice':
                            y_one_hot = pred.data.clone()
                            y_one_hot[...] = 0
                            #y_one_hot = torch.FloatTensor(x.size(0), 2, x.size(2), x.size(3))
                            #y_one_hot = y_one_hot.to(device)
                            y_one_hot.scatter_(1,y.type(torch.LongTensor).to(device),1)


                            loss = dice_loss(torch.log(torch.clamp(pred, 1E-7, 1.0)), y_one_hot)
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
                        print("*** Fold:", fold, "  , Epoch:", epoch, " ***")
                        print("Validation. Mini-batch ", b_v+1, "/", len(training_dataloader))
                        print("Training Loss: {:.4f}, Jacc: {:.4f}".format(loss.item(), batch_jacc))
                        print_line()
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

                        

    #Plot learning curve
    if train_complete:
        plot_learning_curve(train_losses, val_losses, the_title="Learning curve", measure = "Loss (" + options["loss"] + ")", early_stopping = True, filename = jp(path_results, "loss_plot.png"))
        plot_learning_curve(train_jaccs, val_jaccs, the_title="Jaccard plot", measure = "Jaccard", early_stopping = False, filename = jp(path_results, "jaccard_plot.png"))
        save_this([train_losses, val_losses, train_jaccs, val_jaccs], path_results, "losses_values")
    else:
        try:
            lesion_model.load_state_dict(torch.load(jp(path_models,"checkpoint.pt")))
        except:
            raise ValueError("No model found")


    def get_groups(all_patches, total_timepoints, desired_timepoints):
        
        # Squeeze
        all_patches = all_patches.squeeze()

        # Apply time-point padding -> Replicate first and last timepoints in order to sample properly
        all_patches = np.pad(all_patches,((0,0), (1,1), (0,0), (0,0), (0,0), (0,0)), "edge")

        #Slice timepoints 
        output_groups = []
        for i in range(1, tot_timepoints+1):
            output_groups.append(all_patches[:,i-1:i+2, :, :, : ,:])

        return output_groups

    def divide_inference_slices(all_slices, desired_timepoints):
        #Format for all_slices is (num_slices, num_timepoints, num_modalities, Height, Width)
        _, num_timepoints, _, _, _, _ = all_slices.shape
        
        if(num_timepoints == desired_timepoints): #Special case if number of timepoints coincides with desired number of timepoints
            #TODO: special case -> How to predict timepoint in the middle
            pass

        list_inference_patches = []
        indexes = []
        num_forward = num_timepoints - desired_timepoints + 1
        for i in range(num_forward):
            list_inference_patches.append(all_slices[:,i:i+desired_timepoints,:,:,:,:])
            indexes.append(i+desired_timepoints)

        all_slices = np.flip(all_slices, axis = 1).copy()
        num_backward = num_forward
        if(num_timepoints%2 == 0): #even
            init = 0
        else: #odd
            init = 1
        for i in range(init,num_backward):
            list_inference_patches.append(all_slices[:,i:i+desired_timepoints,:,:,:,:])
            if(num_timepoints%2==0): #even
                indexes.append(i+1)
            else:
                indexes.append(i)
        
        sorted_indexes = np.argsort(indexes)
        sorted_list_inference_patches = [list_inference_patches[k] for k in sorted_indexes]

        return sorted_list_inference_patches

    # Free some RAM
    del training_dataset, training_dataloader, validation_dataset, validation_dataloader

    #Evaluate all test images
    columns = compute_metrics(None, None, labels_only=True)
    df = pd.DataFrame(columns = columns)

    test_images = [curr_test_patient]
    i_row=0
    cnt=0
    path_test = options['test_path']
    for case in test_images:
        print(cnt+1, "/", len(test_images))
        scan_path = jp(path_test, case)

        tot_timepoints = len(list_files_with_name_containing(jp(path_test, case), "brain_mask", "nii.gz"))

        infer_patches, coordenates = get_inference_patches(path_test=path_test,
                                                case = case,
                                                input_data=options['input_data'],
                                                roi=options['brain_mask'],
                                                patch_shape=options['patch_size'],
                                                step=options['sampling_step'],
                                                normalize=options['normalize'],
                                                mode = "l",
                                                num_timepoints=tot_timepoints)

        #inf_patches_sets = divide_inference_slices(infer_patches, options['num_timepoints'])
        inf_patches_sets = get_groups(infer_patches, tot_timepoints, options['num_timepoints'])

        batch_size = options['batch_size']

        for i_timepoint in range(len(inf_patches_sets)):
            infer_patches = inf_patches_sets[i_timepoint]

            lesion_out = build_image(infer_patches, lesion_model, device, options['num_classes'], options)

            scan_numpy = nib.load(jp(path_test, case, os.listdir(scan_path)[0])).get_fdata()
            all_probs = np.zeros((scan_numpy.shape[0], scan_numpy.shape[1], scan_numpy.shape[2], options['num_classes']))

            for i in range(options['num_classes']):
                all_probs[:,:,:,i] = reconstruct_image(lesion_out[:,i], 
                                                coordenates, 
                                                scan_numpy.shape)
                                    
            labels = np.argmax(all_probs, axis=3).astype(np.uint8)

            #shim_overlay(scan_numpy, labels, 16, alpha=0.6)

            #Compute metrics
            list_gt = get_dictionary_with_paths([case], path_test, options["gt"])[case]

            labels_gt = nib.load(list_gt[i_timepoint][0]).get_fdata().astype(np.uint8)  #GT  

            #DSC
            #dsc = compute_dices(labels_gt.flatten(), labels.flatten())
            metrics = compute_metrics(labels_gt, labels)

            df.loc[i_row] = list(metrics.values())
            i_row += 1
            #Save result
            img_nib = nib.Nifti1Image(labels, np.eye(4))
            create_folder(jp(path_segmentations, case))
            nib.save(img_nib, jp(path_segmentations, case, case+"_"+ str(i_timepoint+1).zfill(2) +"_segm.nii.gz"))

            cnt += 1

    df.to_csv(jp(path_results, "results.csv"), float_format = '%.5f', index = False)
    print(df.mean())
    create_log(path_results, options)

