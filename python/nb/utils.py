""" Collection of random utilities used in exploratory notebooks """
# TODO: Decide what to do with this -- perhaps move to ./docs somewhere since it isn't crucial?
import os

import tfdecon
from tfdecon import data as tfdecon_data
from tfdecon.data import Acquisition
import numpy as np
import matplotlib.pyplot as plt
from scipy.ndimage.interpolation import rotate
from functools import partial

def plot_zstack_2d(img, ncols=5, in_per_col=3, in_per_row=3, cmap='Greys_r', idx_offset=0):
    n = img.shape[0]
    nrow = int(np.ceil(n / float(ncols)))
    ncol = min(n, ncols)
    fig, axs = plt.subplots(nrow, ncol)
    axs = axs.ravel()
    fig.set_size_inches((in_per_col * ncols, in_per_row * nrow))
    for i in range(n):
        axs[i].imshow(img[i], cmap=cmap)
        axs[i].set_title('Index {}'.format(idx_offset + i))
    for i in range(len(axs)):
        axs[i].axis('off')


def plot_zstack_3d(data, cmap='Greys_r'):
    return ZStackViewer(data, cmap=cmap).run()


class ZStackViewer(object):

    def __init__(self, volume, cmap='Greys_r'):
        self.volume = volume
        self.cmap = cmap

    def run(self):
        """ Use this to launch interactive window for 3D image visualization """
        fig, ax = plt.subplots()
        ax.volume = self.volume
        ax.index = self.volume.shape[0] // 2
        ax.imshow(self.volume[ax.index], cmap=self.cmap)
        ax.set_title('Z-Index ' + str(ax.index))
        fig.canvas.mpl_connect('key_press_event', process_key)
        return fig, ax

def process_key(event):
    fig = event.canvas.figure
    ax = fig.axes[0]
    if event.key == 'j':
        previous_slice(ax)
    elif event.key == 'k':
        next_slice(ax)
    fig.canvas.draw()

def previous_slice(ax):
    """Go to the previous slice."""
    volume = ax.volume
    ax.index = (ax.index - 1) % volume.shape[0]  # wrap around using %
    ax.images[0].set_array(volume[ax.index])
    ax.set_title('Z-Index ' + str(ax.index))

def next_slice(ax):
    """Go to the next slice."""
    volume = ax.volume
    ax.index = (ax.index + 1) % volume.shape[0]
    ax.images[0].set_array(volume[ax.index])
    ax.set_title('Z-Index ' + str(ax.index))


def plot_img_preview(img, zstart=None, zstop=None, cmap='viridis', proj_figsize=(12,4), **kwargs):
    plt.imshow(img.max(axis=0), cmap=cmap)
    plt.gcf().set_size_inches(proj_figsize)
    plt.gca().set_title('Max Projection (Over {} Z-Slices)'.format(img.shape[0]))
    plot_zstack_2d(img[slice(zstart, zstop),:,:], idx_offset=zstart if zstart else 0,
                           cmap=cmap, **kwargs)

def plot_z_projection(img, cmap='viridis', figsize=(12, 4)):
    plt.imshow(img.max(axis=0), cmap=cmap)
    plt.gcf().set_size_inches(figsize)

rotate_xy = partial(rotate, axes=(1,2))
rotate_yz = partial(rotate, axes=(0,1))
rotate_xz = partial(rotate, axes=(0,2))


def plot_rotations(img, projection=lambda img: img.max(axis=0), cmap='viridis', figsize=(12,12)):
    fig, axs = plt.subplots(3, 3)
    fig.set_size_inches(figsize)
    rotate_fns = [rotate_xy, rotate_yz, rotate_xz]
    rotate_angles = [0, 45, 90]
    for i in range(len(rotate_fns)):
        for j in range(len(rotate_angles)):
            im = rotate_fns[i](img, angle=rotate_angles[j])
            im = projection(im)
            axs[i, j].imshow(im, cmap=cmap)


##### Transformation Utilities

def normalize_2d(img, to=65535., epsilon=1e-8):
    """Normalize the sum of pixel intensities in an image to a particular value"""
    if np.any(img < 0):
        raise ValueError('Cannot normalize image with negative pixel intensities')

    isum = img.sum()
    if np.isclose(isum, 0.):
        raise ValueError('Cannot normalize image with intensity sum 0')

    return np.clip(img * (to / isum), epsilon, to)

def normalize_img(data, to=1.):
    epsilon = 1e-8
    return ((data - data.min()) / (data.max() - data.min())).clip(epsilon, 1.)


#### Data Loading Utilities

DATA_DIR_DEFAULT = os.path.expanduser('~/data/research/hammer/deconvolution/data')
DATA_DIR = os.getenv('TFDECON_DATA_DIR', DATA_DIR_DEFAULT)


def set_data_dir(path):
    """Assign data directory manually

    Otherwise, this will be inferred from the environment variable "TFDECON_DATA_DIR" and if
    that is not set will default to `DATA_DIR_DEFAULT`
    Args:
        path: Path containing image data to use for validation and experimentation
    """
    global DATA_DIR
    DATA_DIR = path


def _path(path):
    return os.path.expanduser(os.path.join(DATA_DIR, path))


def load_bars():
    """Get data for "Hollow Bars" dataset"""
    img_tru = tfdecon_data.load_img_stack(_path('bars/Bars/*.tif'))
    img_obs = tfdecon_data.load_img_stack(_path('bars/Bars-G10-P30/*.tif'))
    img_psf = tfdecon_data.load_img_stack(_path('bars/PSF-Bars/*.tif'))
    return Acquisition(img_obs, img_psf, actual=img_tru)


def load_bead():
    """Get data for "Hollow Bars" dataset"""
    img_obs = tfdecon_data.load_img_stack(_path('bead/Bead/*.tif'))
    img_psf = tfdecon_data.load_img_stack(_path('bead/PSF-Bead/*.tif'))
    return Acquisition(img_obs, img_psf, actual=None)


def save_dataset(name, acq, path, dtype=np.float32):
    from skimage import io
    import os

    p = os.path.join(path, name)
    if not os.path.exists(p):
        os.mkdir(p)

    print('Exporting data for dataset "{}" to path {}'.format(name, p))

    io.imsave(os.path.join(p, 'data.tif'), acq.data.astype(dtype))
    io.imsave(os.path.join(p, 'kernel.tif'), acq.kernel.astype(dtype))
    if acq.actual is not None:
        io.imsave(os.path.join(p, 'actual.tif'), acq.actual.astype(dtype))