import os

import numpy as np
import torch
import torch.nn.functional as functional
import sys

from matplotlib import pyplot as plt

import config
import dataset
import network
import nn_utils
import partial_vgg
#import vit


def get_gen_optimizer(vgg_bottom, gen):
    params = list(vgg_bottom.parameters()) + list(gen.parameters())
    return torch.optim.Adam(params, lr=0.00002, betas=(0.5, 0.999))


def get_disc_optimizer(discriminator):
    return torch.optim.Adam(discriminator.parameters(), lr=0.00002, betas=(0.5, 0.999))


def get_gen_criterion():
    kld = torch.nn.KLDivLoss(reduction='batchmean')

    def loss_function(ab, classes, discrim_real, discrim_pred, true_ab, true_labels):
        mse_loss = nn_utils.mse(ab, true_ab)
        kld_loss = kld(torch.log(classes),
                       functional.softmax(true_labels, dim=1))
        wasser_loss = nn_utils.wasserstein_loss(discrim_real) - nn_utils.wasserstein_loss(discrim_pred)
        print("mse: " + str(mse_loss.item()))
        print("kld: " + str(kld_loss.item()))
        print("wasser: " + str(wasser_loss.item()))
        loss = mse_loss \
            + 0.003 * kld_loss \
            + wasser_loss
        return loss

    return loss_function


def get_disc_criterion(device='cpu'):
    def loss_function(real, pred, real_sample, pred_sample, discriminator,
                      gradient_penalty_weight=1):
        real_loss = nn_utils.wasserstein_loss(real)
        pred_loss = nn_utils.wasserstein_loss(pred)
        gp_loss = nn_utils.compute_gradient_penalty(
            discriminator, real_sample, pred_sample, device=device) * gradient_penalty_weight
        # gp_loss = nn_utils.gradient_penalty_loss(avg, random_average_ab, gradient_penalty_weight)

        print("real: " + str(real_loss.item()))
        print("pred: " + str(pred_loss.item()))
        print("grad: " + str(gp_loss.item()))
        return -1 * real_loss + \
            1 * pred_loss + \
            1 * gp_loss

    return loss_function


def generate_from_bw(device, vgg_bottom, unflatten, generator, grey):
    grey_3 = grey.repeat(1, 3, 1, 1).to(device)
    vgg_bottom_out_flat = vgg_bottom(grey_3)
    # To undo the flatten operation in vgg_bottom
    vgg_bottom_out = unflatten(vgg_bottom_out_flat)
    predicted_ab, _ = generator(vgg_bottom_out)
    return predicted_ab


def train_gan(e=None, b=None):
    # Get cpu or gpu device for training.
    device = "cuda" if config.use_gpu and torch.cuda.is_available() else "cpu"
    print(device)

    print("Loading data...")
    train_loader, test_loader, train_len, test_len = dataset.get_loaders()
    print("Loaded")
    save_models_path = os.path.join(config.model_dir)
    if not os.path.exists(save_models_path):
        os.makedirs(save_models_path)
    save_image_path = os.path.join(config.image_dir)
    if not os.path.exists(save_image_path):
        os.makedirs(save_image_path)
    # Load models
    vgg_bottom, unflatten = partial_vgg.get_partial_vgg()
    vgg_bottom, unflatten = vgg_bottom.to(device), unflatten.to(device)
    # Yes it's strange that the bottom gets trained but the top doesn't
    vgg_top = partial_vgg.get_vgg_top().to(device)
    discriminator = network.Discriminator().to(device)
    #discriminator = vit.ViT(
    #    image_size = 224,
    #    patch_size = 8,
    #    num_classes = 2,
    #    dim = 16,
    #    depth = 2,
    #    heads = 2,
    #    mlp_dim = 32,
    #    dropout = 0.1,
    #    emb_dropout = 0.1,
    #    pool='mean').to(device)
    generator = network.Colorization_Model().to(device)

    if e is not None and b is not None:
        e = int(e)
        b = int(b)
        vgg_bottom.load_state_dict(torch.load(
            save_models_path+"vgg_bottom_e"+str(e)+"_b"+str(b)+".pth"))
        discriminator.load_state_dict(torch.load(
            save_models_path+"discriminator_e"+str(e)+"_b"+str(b)+".pth"))
        generator.load_state_dict(torch.load(
            save_models_path+"generator_e"+str(e)+"_b"+str(b)+".pth"))
    else:
        e = 0
        b = 0
    gen_optimizer = get_gen_optimizer(vgg_bottom, generator)
    disc_optimizer = get_disc_optimizer(discriminator)
    gen_criterion = get_gen_criterion()
    disc_criterion = get_disc_criterion(device)

    num_batches = int(len(train_loader) / config.batch_size)
    # torch.autograd.set_detect_anomaly(True)
    with open('logging.txt', 'w') as log:
        sys.stdout = log
        print("New Training Sequence:")
        for epoch in range(e, config.num_epochs):
            print("Training epoch " + str(epoch))
            running_gen_loss = 0.0
            running_disc_loss = 0.0
            for i, data in enumerate(train_loader, 0):
                if i>train_len-b:
                    break
                # Print progress
                log.flush()
                print("Epoch "+str(epoch)+" Batch " + str(i))

                # ab channels of l*a*b color space - is color
                ab, grey = data
                ab, grey = ab.to(device), grey.to(device)
                # Images are in l*a*b* space, normalized

                # Use pre-trained VGG as in original paper
                grey_3 = grey.repeat(1, 3, 1, 1).to(device)
                vgg_bottom_out_flat = vgg_bottom(grey_3)
                # To undo the flatten operation in vgg_bottom
                vgg_bottom_out = unflatten(vgg_bottom_out_flat)
                vgg_out = vgg_top(vgg_bottom_out)
                predicted_ab, predicted_classes = generator(vgg_bottom_out)

                discrim_from_real = discriminator(
                    torch.concat([grey, ab], dim=1))
                discrim_from_predicted = discriminator(
                    torch.concat([grey, predicted_ab], dim=1))

                # Train generator
                gen_loss = gen_criterion(predicted_ab, predicted_classes,discrim_from_real, discrim_from_predicted,
                                         ab, vgg_out)
                gen_optimizer.zero_grad()
                gen_loss.backward(retain_graph=True)
                running_gen_loss = running_gen_loss + gen_loss.detach().item()

                # Train discriminator
                disc_loss = disc_criterion(discrim_from_real, discrim_from_predicted, torch.concat([grey, ab], dim=1),
                                           torch.concat([grey, predicted_ab], dim=1), discriminator)
                disc_optimizer.zero_grad()
                disc_loss.backward()
                running_disc_loss = running_disc_loss + disc_loss.detach().item()

                gen_optimizer.step()
                disc_optimizer.step()
                print("Generator loss: "+str(gen_loss.item()))
                print("Discriminator loss: "+str(disc_loss.item()))

                # Save a demo image after every 50 batches
                if i % 50 == 0:
                    # Reshape dimensions to be as expected
                    processed_ab = torch.squeeze(predicted_ab[0], dim=0)
                    processed_image = dataset.to_image(
                        data[1][0], processed_ab)
                    plt.imsave(save_image_path+"/e" + str(epoch) + "b" +
                               str(i) + ".png", processed_image.numpy())

                # Save the models every 500 batches
                if i % 500 == 499:
                    torch.save(vgg_bottom.state_dict(),
                               save_models_path + "/vgg_bottom_e" + str(epoch) + "_b" + str(i) + ".pth")
                    torch.save(generator.state_dict(),
                               save_models_path + "/generator_e" + str(epoch) + "_b" + str(i) + ".pth")
                    torch.save(discriminator.state_dict(),
                               save_models_path + "/discriminator_e" + str(epoch) + "_b" + str(i) + ".pth")

            b = 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) == 2:
        print("Using epoch and batch from args")
        epoch = args[0]
        batch = args[1]
        train_gan(e=epoch, b=batch)
    else:
        train_gan()
