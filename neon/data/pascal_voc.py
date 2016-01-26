# ----------------------------------------------------------------------------
# Copyright 2015 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
"""
Defines PASCAL_VOC datatset handling.
"""
import numpy as np
import os
import xml.dom.minidom as minidom
from PIL import Image

from neon import NervanaObject
from neon.data.datasets import Dataset
from neon.util.persist import save_obj
from neon.util.persist import load_obj


# background class is always indexed at 0
PASCAL_VOC_CLASSES = ('__background__',
                      'aeroplane', 'bicycle', 'bird', 'boat',
                      'bottle', 'bus', 'car', 'cat', 'chair',
                      'cow', 'diningtable', 'dog', 'horse',
                      'motorbike', 'person', 'pottedplant',
                      'sheep', 'sofa', 'train', 'tvmonitor')
PASCAL_VOC_NUM_CLASSES = 20 + 1  # 20 object classes and 1 background
FRCN_IMG_PER_BATCH = 4
FRCN_ROI_PER_IMAGE = 64
# how many percentage should sample from the foreground obj
FRCN_FG_FRAC = 0.25
FRCN_IOU_THRE = 0.5  # IoU threshold to be considered
FRCN_FG_IOU_THRE = 0.5  # IoU threshold to be considered as foreground obj
# IoU low threshold to be considered as background obj
FRCN_BG_IOU_THRE_LOW = 0.1
FRCN_BG_IOU_THRE_HIGH = 0.5
FRCN_MIN_SCALE = 600  # 600 # the max image scales on the min dim
FRCN_MAX_SCALE = 1000  # 1000 # the max image scales on the max dim

# From Caffe:
# Pixel mean values (BGR order) as a (1, 1, 3) array
# These are the values originally used for training VGG16
# __C.PIXEL_MEANS = np.array([[[102.9801, 115.9465, 122.7717]]])
FRCN_PIXEL_MEANS = np.array([[[102.9801, 115.9465, 122.7717]]])

# the loaded image will be (H, W, C) need to make it (C, H, W)
FRCN_IMG_DIM_SWAP = (2, 0, 1)

FRCN_EPS = 1e-14

dataset_meta = {
    'test-2007': dict(size=460032000,
                      file='VOCtest_06-Nov-2007.tar',
                      url='http://host.robots.ox.ac.uk/pascal/VOC/voc2007',
                      subdir='VOCdevkit/VOC2007'),
    'trainval-2007': dict(size=451020800,
                          file='VOCtrainval_06-Nov-2007.tar',
                          url='http://host.robots.ox.ac.uk/pascal/VOC/voc2007',
                          subdir='VOCdevkit/VOC2007'),
    'trainval-2012': dict(size=2000000000,
                          file='VOCtrainval_11-May-2012.tar',
                          url='http://host.robots.ox.ac.uk/pascal/VOC/voc2012',
                          subdir='VOCdevkit/VOC2012'),
    'selective-search': dict(size=628395563,
                             file='selective_search_data_pkl.tar.gz',
                             url='https://s3-us-west-1.amazonaws.com/nervana-pascal-voc-data',
                             subdir='selective_search_data_pkl')
}


class PASCALVOC(Dataset):

    """
    Construct a PASCAL VOC dataset object.
    For now, it will also load precomputed selective search results as ROIs.

    The structure of VOC is:
        $VOC_ROOT: path/VOCdevkit/VOC2007
        $VOC_ROOT/ImageSet/Main/train.txt (or test.txt etc.): image index file
        $VOC_ROOT/Annotations/*.xml: classes and bb for each image

    Notes:
        1. ground truth bounding rp are 1-based pixel coordinates, need to
            make it 0-based for input data
        2. bounding box coordinate: (x_min, y_min, x_max, y_max)

    Args:
        image_set (str) : 'trainval' or 'test'
        year (String) : e.g. '2007'
        path (String) : Path to data file
        add_flipped (Bool) : whether to enhance the dataset with flipped images
        overlap_thre (Float): the IOU threshold of bbox to be used for training
        output_type (Int, optional): the type of data iterator will yield, to
                                    provide data for FRCN or its variants
                    0 (normal FRCN model) -- X: (image, rois) Y: (labels, (bb targets,bb mask))
                    1 (label stream with ROI) -- X: (image, rois) Y: (labels)
                    2 (label stream no ROI) -- X: image Y: labels
        n_mb (Int, optional): how many minibatch to iterate through, can use
                                value smaller than nbatches for debugging
        img_per_batch (Int, optional): how many images processed per batch
        rois_per_img (Int, optional): how many rois to pool from each image
        rois_random_sample  (Bool, optional): randomly sample the ROIs. Default
                                                to be true

        """

    def __init__(self, image_set, year, path='.', add_flipped=False,
                 overlap_thre=None, output_type=0, n_mb=None, img_per_batch=None,
                 rois_per_img=None, rois_random_sample=True, shuffle=False):
        self.isRoiDB = True
        self.batch_index = 0
        self.year = year
        self.image_set = image_set
        self.add_flipped = add_flipped
        self.overlap_thre = overlap_thre if overlap_thre else FRCN_IOU_THRE
        self.output_type = output_type

        # how many ROIs per image
        self.rois_per_image = rois_per_img if rois_per_img else FRCN_ROI_PER_IMAGE
        self.img_per_batch = img_per_batch if img_per_batch else FRCN_IMG_PER_BATCH
        self.fg_rois_per_image = FRCN_FG_FRAC * self.rois_per_image
        self.bg_rois_per_image = self.rois_per_image - self.fg_rois_per_image
        self.rois_per_batch = self.rois_per_image * self.img_per_batch
        self.rois_random_sample = rois_random_sample
        self.shuffle = shuffle

        self.cache_file_name = 'voc_{}_{}_flip_{}_ovlp_{}.pkl'.format(self.year,
                                                                      self.image_set,
                                                                      self.add_flipped,
                                                                      self.overlap_thre)
        print 'prepare PASCAL VOC {} from year {}: add flipped image {} and overlap threshold {}'\
            .format(self.image_set, self.year, self.add_flipped, self.overlap_thre)

        # PASCAL class to index
        self.num_classes = PASCAL_VOC_NUM_CLASSES
        self._class_to_index = dict(
            zip(PASCAL_VOC_CLASSES, xrange(self.num_classes)))

        # load the voc dataset
        self.voc_root = self.load_voc(image_set, year, path)

        self.cache_file = os.path.join(self.voc_root, self.cache_file_name)

        # load the precomputed ss results from voc data, it includes both 2007 and 2012 data
        self.ss_path = self.load_voc('ss', None, path)

        # VOC paths and infos
        self.image_index_file = os.path.join(self.voc_root, 'ImageSets', 'Main',
                                             self.image_set + '.txt')
        self.image_path = os.path.join(self.voc_root, 'JPEGImages')
        self._image_file_ext = '.jpg'

        self.annotation_path = os.path.join(self.voc_root, 'Annotations')
        self._annotation_file_ext = '.xml'
        self._annotation_obj_tag = 'object'
        self._annotation_class_tag = 'name'
        self._annotation_xmin_tag = 'xmin'
        self._annotation_xmax_tag = 'xmax'
        self._annotation_ymin_tag = 'ymin'
        self._annotation_ymax_tag = 'ymax'

        self._selective_search_ext = '.pkl'
        self.selective_search_file = os.path.join(
            self.ss_path,
            '_'.join(['voc', year, self.image_set, 'selectivesearch.pkl']))

        self._bb_xmin_idx = 0
        self._bb_ymin_idx = 1
        self._bb_xmax_idx = 2
        self._bb_ymax_idx = 3

        # self.rois_per_batch is 128 (2*64) ROIs
        # But the image path batch size is self.img_per_batch
        # need to control the batch size here
        print "Backend batchsize is changed to be image_per_batch from PASCAL_VOC dataset"
        self.be.bsz = self.img_per_batch

        # backend tensor to push the data
        self.image_shape = (3, FRCN_MAX_SCALE, FRCN_MAX_SCALE)
        self.img_np = np.zeros(
            (3, FRCN_MAX_SCALE, FRCN_MAX_SCALE, self.be.bsz), dtype=np.float32)
        self.dev_X_img = self.be.iobuf(self.image_shape, dtype=np.float32)
        self.dev_X_img_chw = self.dev_X_img.reshape(
            3, FRCN_MAX_SCALE, FRCN_MAX_SCALE, self.be.bsz)
        # for rois, features are 4 + 1 (idx within the batch)
        self.dev_X_rois = self.be.zeros((self.rois_per_batch, 5))
        self.dev_y_labels_flat = self.be.zeros(
            (1, self.rois_per_batch), dtype=np.int32)
        self.dev_y_labels = self.be.zeros(
            (self.num_classes, self.rois_per_batch), dtype=np.int32)
        self.dev_y_bbtargets = self.be.zeros(
            (self.num_classes * 4, self.rois_per_batch))
        self.dev_y_bbmask = self.be.zeros(
            (self.num_classes * 4, self.rois_per_batch))

        # the shape will indicate the shape for 1st path (ImageNet model), and
        # 2nd path (ROIs)
        self.shape = [self.image_shape, self.num_classes * 4]

        # Need to do the following:
        #   1. load the image index list
        #   2. for each image, load the ground truth from pascal annotation
        #   3. load the selective search ROIs (this step needs gt ROIs)
        #   4.1. merge the ROIs
        #   4.2. may have to add the flipped images for training
        #   4.3. add the fields for max overlap and max overlapped classes
        #   4.4. add the bounding box targets for regression
        #   5. during minibatch feeding:
        #   - rescale images
        #   - rescale ROIs
        #   - random select foreground ROIs (bigger ones)
        #   - random select background ROIS (smaller ones)
        #   - clamp bg ROI labels (to be 0)
        #   - convert ROIs into the regression target (ROIs, 4*21)

        # 1.
        assert os.path.exists(self.image_index_file), \
            'Image index file does not exist: {}'.format(self.image_index_file)
        with open(self.image_index_file) as f:
            self.image_index = [x.strip() for x in f.readlines()]

        # self.image_index = image_index * 2 if self.add_flipped else image_index
        self.num_images = len(self.image_index)
        self.num_image_entries = self.num_images * \
            2 if self.add_flipped else self.num_images
        self.ndata = self.num_image_entries * self.rois_per_image
        self.nbatches = self.num_image_entries/self.img_per_batch

        if n_mb is not None:
            self.nbatches = n_mb

        if os.path.exists(self.cache_file):
            self.roi_db = load_obj(self.cache_file)
            print 'ROI dataset loaded from file {}'.format(self.cache_file)
        else:
            # 2.
            self.roi_gt = self.load_pascal_roi_groundtruth()

            # 3.
            self.roi_ss = self.load_pascal_roi_selectivesearch()

            # 4.
            self.roi_db = self.combine_gt_ss_roi()

            save_obj(self.roi_db, self.cache_file)
            print 'wrote ROI dataset to {}'.format(self.cache_file)

    def load_voc(self, dataset, year=None, path="."):

        """
        dataset: 'trainval', 'test', or 'ss'
        year: 2007 or 2012 if not 'ss', otherwise None

        For selective search data
        Fetch the pre-computed selective search data which are converted from
        the MAT files available from
        http://www.cs.berkeley.edu/~rbg/fast-rcnn-data/selective_search_data.tgz
        """

        dataset = 'selective-search' if year is None else '-'.join([dataset, year])
        voc = dataset_meta[dataset]
        workdir, filepath, datadir = self._valid_path_append(path, '', voc['file'], voc['subdir'])

        if not os.path.exists(filepath):
            self.fetch_dataset(voc['url'], voc['file'], filepath, voc['size'])

        if not os.path.exists(datadir):
            with tarfile.open(filepath) as f:
                f.extractall(workdir)

        return datadir

    def __iter__(self):
        """
        Generator that can be used to iterate over this dataset.

        Each minibatch is constructed from self.img_per_batch images,
                                        and FRCN_BATCH_SIZE ROIs

        1. At the begining of the epoch, shuffle the dataset instances
        2. For each minibatch, sample the ROIs from each image

        Yields:
            tuples, tuples, first tuple contains image that goes into an ImageNet model
                            and ROI data
                            second tuple contains class labels for each ROIs and
                            bounding box regression targets
        """
        self.batch_index = 0

        # permute the dataset each epoch
        if self.shuffle is False:
            shuf_idx = range(self.num_image_entries)
        else:
            shuf_idx = self.be.rng.permutation(self.num_image_entries)

        for self.batch_index in xrange(self.nbatches):
            start = self.batch_index * self.img_per_batch
            end = (self.batch_index + 1) * self.img_per_batch

            db_inds = shuf_idx[start:end]

            mb_db = [self.roi_db[i] for i in db_inds]

            rois_mb = np.zeros((self.rois_per_batch, 5), dtype=np.float32)
            labels_blob = np.zeros((self.rois_per_batch), dtype=np.int32)
            bbox_targets_blob = np.zeros((self.rois_per_batch, 4 * self.num_classes),
                                         dtype=np.float32)
            bbox_loss_blob = np.zeros(
                bbox_targets_blob.shape, dtype=np.float32)
            self.img_np[:] = 0

            for im_i, db in enumerate(mb_db):

                # load and process the image using PIL
                im = Image.open(db['img_file'])  # This is RGB order

                im_shape = np.array(im.size, np.int32)
                im_size_min = np.min(im_shape)
                im_size_max = np.max(im_shape)
                im_scale = float(FRCN_MIN_SCALE) / float(im_size_min)
                # Prevent the biggest axis from being more than FRCN_MAX_SCALE
                if np.round(im_scale * im_size_max) > FRCN_MAX_SCALE:
                    im_scale = float(FRCN_MAX_SCALE) / float(im_size_max)
                im_shape = (im_shape * im_scale).astype(int)
                im = im.resize(im_shape, Image.LINEAR)

                # load it to numpy and flip the channel RGB to BGR
                im = np.array(im)[:, :, ::-1]
                if db['flipped']:
                    im = im[:, ::-1, :]
                # Mean subtract and scale an image
                im = im.astype(np.float32, copy=False)
                im -= FRCN_PIXEL_MEANS

                # Sample fore-ground and back-ground ROIs from the proposals and labels
                labels, overlaps, im_rois, bbox_targets, bbox_loss \
                    = _sample_fg_bg_rois(db, self.fg_rois_per_image, self.rois_per_image,
                                         self.num_classes, self.rois_random_sample)

                # Add to RoIs blob
                rois = im_rois * im_scale
                num_rois_this_image = rois.shape[0]
                slice_i = slice(im_i * self.rois_per_image,
                                im_i * self.rois_per_image + num_rois_this_image)
                batch_ind = im_i * np.ones((num_rois_this_image, 1))
                # add the corresponding image ind (within this batch) to the ROI data
                rois_this_image = np.hstack((batch_ind, rois))

                rois_mb[slice_i] = rois_this_image

                # Add to labels, bbox targets, and bbox loss blobs
                labels_blob[slice_i] = labels.ravel()
                bbox_targets_blob[slice_i] = bbox_targets
                bbox_loss_blob[slice_i] = bbox_loss

                # write it to backend tensor
                self.img_np[:, :im_shape[1], :im_shape[0], im_i] = im.transpose(FRCN_IMG_DIM_SWAP)

            self.dev_X_img_chw.set(self.img_np)
            self.dev_X_rois[:] = rois_mb
            self.dev_y_labels_flat[:] = labels_blob.reshape(1, -1)
            self.dev_y_labels[:] = self.be.onehot(
                self.dev_y_labels_flat, axis=0)
            self.dev_y_bbtargets[:] = bbox_targets_blob.T.astype(
                np.float, order='C')
            self.dev_y_bbmask[:] = bbox_loss_blob.T.astype(np.int32, order='C')

            if self.output_type == 0:
                X = (self.dev_X_img, self.dev_X_rois)
                Y = (self.dev_y_labels, (self.dev_y_bbtargets, self.dev_y_bbmask))
            elif self.output_type == 1:
                X = (self.dev_X_img, self.dev_X_rois)
                Y = self.dev_y_labels
            elif self.output_type == 2:
                X = self.dev_X_img
                Y = self.dev_y_labels
            else:
                raise ValueError(
                    'Do not support output_type to be {}'.format(self.output_type))

            yield X, Y

    def reset(self):
        """
        For resetting the starting index of this dataset back to zero.
        """
        self.batch_index = 0

    def load_pascal_roi_groundtruth(self):
        """
        load the voc database ground truth ROIs
        """

        return [self.load_pascal_annotation(img) for img in self.image_index]

    def load_pascal_annotation(self, image_index):
        """
        For a particular image, load ground truth annotations of object classes
        and their bounding rp from the pascal voc dataset files are in the
        VOC directory/Annotations. Each xml file corresponds to a particular
        image index
        """
        annotation_file = os.path.join(self.annotation_path,
                                       image_index + self._annotation_file_ext)
        with open(annotation_file) as f:
            annotation_data = minidom.parseString(f.read())

        # how many objects in it
        objs = annotation_data.getElementsByTagName(self._annotation_obj_tag)
        num_objs = len(objs)

        # initialize ground truth classes and bb
        gt_bb = np.zeros((num_objs, 4), dtype=np.uint16)
        gt_classes = np.zeros((num_objs, 1), dtype=np.int32)
        gt_overlaps = np.zeros((num_objs, self.num_classes), dtype=np.float32)
        gt_max_overlap = np.zeros((num_objs, 1))
        gt_max_class = np.zeros((num_objs, 1))

        # load all the info
        for idx, obj in enumerate(objs):
            x1 = float(load_data_from_xml_tag(obj, self._annotation_xmin_tag)) - 1
            y1 = float(load_data_from_xml_tag(obj, self._annotation_ymin_tag)) - 1
            x2 = float(load_data_from_xml_tag(obj, self._annotation_xmax_tag)) - 1
            y2 = float(load_data_from_xml_tag(obj, self._annotation_ymax_tag)) - 1
            cls = self._class_to_index[
                str(load_data_from_xml_tag(obj, self._annotation_class_tag)).
                lower().strip()]
            gt_bb[idx] = [x1, y1, x2, y2]
            gt_classes[idx] = cls
            gt_overlaps[idx, cls] = 1.0
            gt_max_overlap[idx] = 1.0
            gt_max_class[idx] = cls

        gt_bb_target = np.zeros((num_objs, 5))
        gt_bb_target[:, 0] = gt_max_class.ravel()

        return {'gt_bb': gt_bb,
                'gt_classes': gt_classes,
                'gt_overlaps': gt_overlaps,
                'img_id': image_index,
                'max_overlap_area': gt_max_overlap,
                'max_overlap_class': gt_max_class,
                'bb_targets': gt_bb_target,
                }

    def load_pascal_roi_selectivesearch(self):
        """
        Load the pre-computed selective search data on PASCAL VOC in MAT file

        The MAT file contains images and rp:
            images: image indices for the dataset (Img, 1)
                    name in string is in images[i][0][0]
            rp: all the proposed ROIs for each image (Img, 1)
                    in bb[i], there are (B, 4) for B proposed ROIs
                    The coordinates are ordered as:
                    [y1, x1, y2, x2]
                    While ground truth coordinates are:
                    [x1, y1, x2, y2]
                    So it needs re-ordering

        """
        assert self.roi_gt is not None, 'Ground truth ROIs need to be loaded first'
        assert os.path.exists(self.selective_search_file), \
            'selected search data does not exist'

        ss_data = load_obj(self.selective_search_file)
        ss_bb = ss_data['boxes'].ravel()
        ss_img_idx = ss_data['images'].ravel()
        ss_num_img = ss_bb.shape[0]

        assert ss_num_img == self.num_images, \
            'Number of images in SS data must match number of image in the dataset'

        roi_ss = []
        # load the bb from SS and compare with gt
        for i in xrange(ss_num_img):
            # make sure the image index match
            assert self.image_index[i] == ss_img_idx[i][0]
            bb = (ss_bb[i][:, (1, 0, 3, 2)] - 1)
            num_boxes = bb.shape[0]
            overlaps = np.zeros(
                (num_boxes, self.num_classes), dtype=np.float32)

            gt_bb = self.roi_gt[i]['gt_bb']
            gt_classes = self.roi_gt[i]['gt_classes'].ravel()

            gt_overlap, gt_dim = calculate_bb_overlap(bb.astype(np.float),
                                                      gt_bb.astype(np.float))

            max_overlap_area = gt_overlap.max(axis=gt_dim)
            max_overlap_arg = gt_overlap.argmax(axis=gt_dim)

            # only put the non-zero overlaps into the table
            I = np.where(max_overlap_area > 0)[0]
            overlaps[I, gt_classes[max_overlap_arg[I]]] = max_overlap_area[I]
            max_overlap_class = overlaps.argmax(axis=gt_dim)
            max_overlaps = overlaps.max(axis=gt_dim)

            # prepare the bounding box targets
            ss_bb_targets = np.zeros((num_boxes, 5), np.float32)
            # only the ones with large enough overlap with gt are used
            use_idx = np.where(max_overlaps >= self.overlap_thre)[0]

            bb_targets = self._compute_bb_targets(gt_bb[max_overlap_arg[use_idx]],
                                                  bb[use_idx],
                                                  max_overlap_class[use_idx])

            ss_bb_targets[use_idx] = bb_targets

            roi_ss.append({
                'ss_bb': bb,
                'gt_classes': np.zeros((num_boxes, 1), dtype=np.int32),
                'gt_overlaps': overlaps,
                'max_overlap_area': max_overlap_area.reshape(-1, 1),
                'max_overlap_class': max_overlap_class.reshape(-1, 1),
                'bb_targets': ss_bb_targets,
            })

        return roi_ss

    def combine_gt_ss_roi(self):
        assert len(self.roi_gt) == len(self.roi_ss) == self.num_images, \
            'ROIs from GT and SS do not match the dataset images'

        # Compute values needed for means and stds
        class_counts = np.zeros((self.num_classes, 1), ) + FRCN_EPS
        sums = np.zeros((self.num_classes, 4))
        squared_sums = np.zeros((self.num_classes, 4))

        roi_gt_ss = [None] * self.num_image_entries
        for i in xrange(self.num_images):
            roi_gt_ss[i] = {}
            roi_gt_ss[i]['bb'] = np.vstack((self.roi_gt[i]['gt_bb'],
                                            self.roi_ss[i]['ss_bb']))
            roi_gt_ss[i]['gt_classes'] = np.vstack((self.roi_gt[i]['gt_classes'],
                                                    self.roi_ss[i]['gt_classes']))
            roi_gt_ss[i]['gt_overlaps'] = np.vstack([self.roi_gt[i]['gt_overlaps'],
                                                     self.roi_ss[i]['gt_overlaps']])
            roi_gt_ss[i]['max_overlap_area'] = np.vstack([self.roi_gt[i]['max_overlap_area'],
                                                          self.roi_ss[i]['max_overlap_area']])
            roi_gt_ss[i]['max_overlap_class'] = np.vstack([self.roi_gt[i]['max_overlap_class'],
                                                           self.roi_ss[i]['max_overlap_class']])
            roi_gt_ss[i]['img_id'] = self.roi_gt[i]['img_id']
            roi_gt_ss[i]['flipped'] = False

            image_file = os.path.join(self.image_path,
                                      self.roi_gt[i]['img_id'] + self._image_file_ext)

            roi_gt_ss[i]['img_file'] = image_file

            # add bounding box targets for training
            bb_targets = np.vstack([self.roi_gt[i]['bb_targets'],
                                    self.roi_ss[i]['bb_targets']])

            roi_gt_ss[i]['bb_targets'] = bb_targets

            for cls in xrange(1, self.num_classes):
                cls_inds = np.where(bb_targets[:, 0] == cls)[0]
                if cls_inds.size > 0:
                    class_counts[cls] += cls_inds.size
                    sums[cls, :] += bb_targets[cls_inds, 1:].sum(axis=0)
                    squared_sums[cls, :] += (bb_targets[cls_inds, 1:] ** 2).sum(axis=0)

            if self.add_flipped:
                width = Image.open(image_file).size[0]
                fliped_bb = roi_gt_ss[i]['bb'].copy()
                fliped_bb[:, self._bb_xmin_idx] = width - \
                    roi_gt_ss[i]['bb'][:, self._bb_xmax_idx] - 1
                fliped_bb[:, self._bb_xmax_idx] = width - \
                    roi_gt_ss[i]['bb'][:, self._bb_xmin_idx] - 1
                bb_targets_flipped = bb_targets
                bb_targets_flipped[:, 1] *= -1

                roi_gt_ss[i + self.num_images] = {
                    'bb': fliped_bb,
                    'gt_classes': roi_gt_ss[i]['gt_classes'],
                    'gt_overlaps': roi_gt_ss[i]['gt_overlaps'],
                    'max_overlap_area': roi_gt_ss[i]['max_overlap_area'],
                    'max_overlap_class': roi_gt_ss[i]['max_overlap_class'],
                    'img_id': roi_gt_ss[i]['img_id'],
                    'flipped': True,
                    'img_file': image_file,
                    'bb_targets': bb_targets_flipped
                }
                for cls in xrange(1, self.num_classes):
                    cls_inds = np.where(bb_targets[:, 0] == cls)[0]
                    if cls_inds.size > 0:
                        class_counts[cls] += cls_inds.size
                        sums[cls, :] += bb_targets_flipped[cls_inds, 1:].sum(axis=0)
                        squared_sums[
                            cls, :] += (bb_targets_flipped[cls_inds, 1:] ** 2).sum(axis=0)

        means = sums / class_counts
        stds = np.sqrt(squared_sums / class_counts - means ** 2)

        # Normalize targets
        for i in xrange(self.num_images):
            targets = roi_gt_ss[i]['bb_targets']
            for cls in xrange(1, self.num_classes):
                cls_inds = np.where(targets[:, 0] == cls)[0]
                roi_gt_ss[i]['bb_targets'][cls_inds, 1:] -= means[cls, :]
                roi_gt_ss[i]['bb_targets'][cls_inds, 1:] /= stds[cls, :]

        return roi_gt_ss

    def _compute_bb_targets(self, gt_bb, rp_bb, labels):

        rp_widths = rp_bb[:, 2] - rp_bb[:, 0] + FRCN_EPS
        rp_heights = rp_bb[:, 3] - rp_bb[:, 1] + FRCN_EPS
        rp_ctr_x = rp_bb[:, 0] + 0.5 * rp_widths
        rp_ctr_y = rp_bb[:, 1] + 0.5 * rp_heights

        gt_widths = gt_bb[:, 2] - gt_bb[:, 0] + FRCN_EPS
        gt_heights = gt_bb[:, 3] - gt_bb[:, 1] + FRCN_EPS
        gt_ctr_x = gt_bb[:, 0] + 0.5 * gt_widths
        gt_ctr_y = gt_bb[:, 1] + 0.5 * gt_heights

        targets_dx = (gt_ctr_x - rp_ctr_x) / rp_widths
        targets_dy = (gt_ctr_y - rp_ctr_y) / rp_heights
        targets_dw = np.log(gt_widths / rp_widths)
        targets_dh = np.log(gt_heights / rp_heights)

        targets = np.concatenate((labels[:, np.newaxis],
                                  targets_dx[:, np.newaxis],
                                  targets_dy[:, np.newaxis],
                                  targets_dw[:, np.newaxis],
                                  targets_dh[:, np.newaxis],
                                  ), axis=1)

        return targets


def calculate_bb_overlap(rp, gt):
    """
    calculate the overlaps between 2 list of bounding rp

    Arguments:
        rp (list): an array of region proposals, shape (R, 4)
        gt (list): an array of ground truth ROIs, shape (G, 4)
    Outputs:
        overlaps: a matrix of overlaps between 2 list, shape (R, G)
    """
    gt_dim = 1
    R = rp.shape[0]
    G = gt.shape[0]
    overlaps = np.zeros((R, G), dtype=np.float32)

    for g in range(G):
        gt_box_area = float(
            (gt[g, 2] - gt[g, 0] + 1) *
            (gt[g, 3] - gt[g, 1] + 1)
        )
        for r in range(R):
            iw = float(
                min(rp[r, 2], gt[g, 2]) -
                max(rp[r, 0], gt[g, 0]) + 1
            )
            if iw > 0:
                ih = float(
                    min(rp[r, 3], gt[g, 3]) -
                    max(rp[r, 1], gt[g, 1]) + 1
                )
                if ih > 0:
                    ua = float(
                        (rp[r, 2] - rp[r, 0] + 1) *
                        (rp[r, 3] - rp[r, 1] + 1) +
                        gt_box_area - iw * ih
                    )
                    overlaps[r, g] = iw * ih / ua
    return overlaps, gt_dim


def _sample_fg_bg_rois(roidb, fg_rois_per_image, rois_per_image, num_classes, randomness):
    """Generate a random sample of RoIs comprising foreground and background
    examples.
    """
    # label = class RoI has max overlap with
    labels = roidb['max_overlap_class']
    overlaps = roidb['max_overlap_area']
    rois = roidb['bb']

    # Select foreground RoIs as those with >= FG_THRESH overlap
    fg_inds = np.where(overlaps >= FRCN_FG_IOU_THRE)[0]

    # Guard against the case when an image has fewer than fg_rois_per_image
    # foreground RoIs
    fg_rois_per_this_image = int(np.minimum(fg_rois_per_image, fg_inds.size))
    # Sample foreground regions without replacement
    if fg_inds.size > 0:
        if randomness is True:
            fg_inds = np.random.choice(fg_inds, size=fg_rois_per_this_image,
                                       replace=False)
        else:
            fg_inds = fg_inds[range(fg_rois_per_this_image)]

    # Select background RoIs as those within [FRCN_BG_IOU_THRE_LOW, FRCN_BG_IOU_THRE_HIGH)
    bg_inds = np.where((overlaps < FRCN_BG_IOU_THRE_HIGH) & (overlaps >= FRCN_BG_IOU_THRE_LOW))[0]
    # Compute number of background RoIs to take from this image (guarding
    # against there being fewer than desired)
    bg_rois_per_this_image = int(np.minimum(rois_per_image - fg_rois_per_this_image,
                                            bg_inds.size))
    # Sample foreground regions without replacement
    if bg_inds.size > 0:
        if randomness is True:
            bg_inds = np.random.choice(bg_inds, size=bg_rois_per_this_image,
                                       replace=False)
        else:
            bg_inds = bg_inds[range(bg_rois_per_this_image)]

    # The indices that we're selecting (both fg and bg)
    keep_inds = np.append(fg_inds, bg_inds)
    # Select sampled values from various arrays:
    labels = labels[keep_inds]
    # Clamp labels for the background RoIs to 0
    labels[fg_rois_per_this_image:] = 0
    overlaps = overlaps[keep_inds]
    rois = rois[keep_inds]

    bbox_targets, bbox_loss_weights = \
        _get_bbox_regression_labels(roidb['bb_targets'][keep_inds, :],
                                    num_classes)

    return labels, overlaps, rois, bbox_targets, bbox_loss_weights


def _get_bbox_regression_labels(bbox_target_data, num_classes):
    """Bounding-box regression targets are stored in a compact form in the
    roidb.

    This function expands those targets into the 4-of-4*K representation used
    by the network (i.e. only one class has non-zero targets). The loss weights
    are similarly expanded.

    Returns:
        bbox_target_data (ndarray): N x 4K blob of regression targets
        bbox_loss_weights (ndarray): N x 4K blob of loss weights
    """
    clss = bbox_target_data[:, 0]
    bbox_targets = np.zeros((clss.size, 4 * num_classes), dtype=np.float32)
    bbox_loss_weights = np.zeros(bbox_targets.shape, dtype=np.float32)
    inds = np.where(clss > 0)[0]
    for ind in inds:
        cls = clss[ind]
        start = 4 * cls
        end = start + 4
        bbox_targets[ind, start:end] = bbox_target_data[ind, 1:]
        bbox_loss_weights[ind, start:end] = [1., 1., 1., 1.]
    return bbox_targets, bbox_loss_weights


def load_data_from_xml_tag(element, tag):
    return element.getElementsByTagName(tag)[0].childNodes[0].data
