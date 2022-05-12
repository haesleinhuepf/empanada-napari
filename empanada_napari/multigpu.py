import os
import zarr
import numpy as np
from collections import deque

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
from tqdm import tqdm

from empanada.data import VolumeDataset
from empanada.inference import filters
from empanada.inference.engines import PanopticDeepLabRenderEngine
from empanada.inference.tracker import InstanceTracker
from empanada.inference.matcher import RLEMatcher
from empanada.inference.postprocess import factor_pad, merge_semantic_and_instance
from empanada.array_utils import put
from empanada.inference.rle import pan_seg_to_rle_seg, rle_seg_to_pan_seg
from empanada.zarr_utils import *

from napari.qt.threading import thread_worker

from empanada_napari.utils import Preprocessor

MODEL_DIR = os.path.join(os.path.expanduser('~'), '.empanada')
torch.hub.set_dir(MODEL_DIR)

#----------------------------------------------------------
# Utilities for all gathering outputs from each GPU process
#----------------------------------------------------------

def get_world_size() -> int:
    if not dist.is_available():
        return 1
    if not dist.is_initialized():
        return 1
    return dist.get_world_size()


def all_gather(tensor, group=None):
    # all tensors are same size
    world_size = dist.get_world_size()

    # receiving Tensor from all ranks
    tensor_list = [
        torch.zeros_like(tensor) for _ in range(world_size)
    ]

    dist.all_gather(tensor_list, tensor, group=group)

    return tensor_list

#----------------------------------------------------------
# Process/worker functions
#----------------------------------------------------------

class MedianQueue:
    def __init__(self, qlen=3):
        self.sem_queue = deque(maxlen=qlen)
        self.cell_queue = deque(maxlen=qlen)

        self.qlen = qlen
        self.mid_idx = (qlen - 1) // 2

    def append(self, sem, cells):
        self.sem_queue.append(sem)
        self.cell_queue.append(cells)

    def get_median_sem(self):
        median_sem = torch.median(
            torch.cat(list(self.sem_queue), dim=0), dim=0, keepdim=True
        ).values
        return median_sem

    def get_next(self):
        nq = len(self.sem_queue)
        if nq <= self.mid_idx:
            # take last item in the queue
            sem = self.sem_queue[-1]
            cells = self.cell_queue[-1]
        elif nq > self.mid_idx and nq < self.qlen:
            # nothing to return while queue builds
            return None, None
        else:
            # nq == median_kernel_size
            # use the middle item in the queue
            # with the median segmentation probs
            sem = self.get_median_sem()
            cells = self.cell_queue[self.mid_idx]

        return sem, cells

    def __iter__(self):
        for sem,cells in zip(self.sem_queue, self.cell_queue):
            yield (sem, cells)

def harden_seg(sem, confidence_thr):
    if sem.size(1) > 1: # multiclass segmentation
        sem = torch.argmax(sem, dim=1, keepdim=True)
    else:
        sem = (sem >= confidence_thr).long() # need integers not bool

    return sem

def get_panoptic_seg(sem, instance_cells, config):
    label_divisor = config['engine_params']['label_divisor']
    thing_list = config['engine_params']['thing_list']
    stuff_area = config['engine_params']['stuff_area']
    void_label = config['engine_params']['void_label']

    # keep only label for instance classes
    instance_seg = torch.zeros_like(sem)
    for thing_class in thing_list:
        instance_seg[sem == thing_class] = 1

    # map object ids
    instance_seg = (instance_seg * instance_cells).long()

    pan_seg = merge_semantic_and_instance(
        sem, instance_seg, label_divisor, thing_list,
        stuff_area, void_label
    )

    return pan_seg

def run_forward_matchers(
    config,
    matchers,
    queue,
    rle_stack,
    matcher_in,
    end_signal='finish',
):
    r"""Run forward matching of instances between slices in a separate process
    on CPU while model is performing inference on GPU.
    """
    # create the queue for sem and instance cells
    confidence_thr = config['engine_params']['confidence_thr']
    median_kernel_size = config['engine_params']['median_kernel_size']
    median_queue = MedianQueue(median_kernel_size)

    while True:
        sem, cells = queue.get()
        if sem == end_signal:
            # all images have been matched!
            break

        # update the queue
        median_queue.append(sem, cells)
        median_sem, cells = median_queue.get_next()

        # get segmentation if not None
        if median_sem is not None:
            median_sem = harden_seg(median_sem, confidence_thr)
            pan_seg = get_panoptic_seg(median_sem, cells, config)
        else:
            pan_seg = None
            continue

        # convert pan seg to rle
        pan_seg = pan_seg.squeeze().cpu().numpy()
        rle_seg = pan_seg_to_rle_seg(
            pan_seg, config['labels'],
            config['engine_params']['label_divisor'],
            config['engine_params']['thing_list'],
            config['force_connected']
        )

        # match the rle seg for each class
        for matcher in matchers:
            class_id = matcher.class_id
            if matcher.target_rle is None:
                matcher.initialize_target(rle_seg[class_id])
            else:
                rle_seg[class_id] = matcher(rle_seg[class_id])

        rle_stack.append(rle_seg)

    # get the final segmentations from the queue
    for i, (sem,cells) in enumerate(median_queue):
        if i >= median_queue.mid_idx + 1:
            sem = harden_seg(sem, confidence_thr)
            pan_seg = get_panoptic_seg(sem, cells, config)

            pan_seg = pan_seg.squeeze().cpu().numpy()
            rle_seg = pan_seg_to_rle_seg(
                pan_seg, config['labels'],
                config['engine_params']['label_divisor'],
                config['engine_params']['thing_list'],
                config['force_connected']
            )

            # match the rle seg for each class
            for matcher in matchers:
                class_id = matcher.class_id
                if matcher.target_rle is None:
                    matcher.initialize_target(rle_seg[class_id])
                else:
                    rle_seg[class_id] = matcher(rle_seg[class_id])

            rle_stack.append(rle_seg)

    matcher_in.send([rle_stack])
    matcher_in.close()

def main_worker(gpu, volume, axis_name, rle_stack, rle_out, config):
    config['gpu'] = gpu
    rank = gpu
    axis = config['axes'][axis_name]

    dist.init_process_group(backend='nccl', init_method='tcp://localhost:10001',
                            world_size=config['world_size'], rank=rank)

    engine_cls = PanopticDeepLabRenderEngine
    model = load_model_to_device(config['model_url'], torch.device(f'cuda:{gpu}'))

    preprocessor = Preprocessor(**config['norms'])

    # create the dataloader
    shape = volume.shape
    upsampling = config['inference_scale']
    dataset = VolumeDataset(volume, axis, preprocessor, scale=upsampling)
    sampler = DistributedSampler(dataset, shuffle=False)
    dataloader = DataLoader(
        dataset, batch_size=1, shuffle=False, pin_memory=True,
        drop_last=False, num_workers=0, sampler=sampler
    )

    # if in main process, create matchers and process
    if rank == 0:
        thing_list = config['engine_params']['thing_list']
        matchers = [
            RLEMatcher(thing_class, **config['matcher_params'])
            for thing_class in thing_list
        ]

        queue = mp.Queue()
        matcher_out, matcher_in = mp.Pipe()
        matcher_proc = mp.Process(
            target=run_forward_matchers,
            args=(config, matchers, queue, rle_stack, matcher_in)
        )
        matcher_proc.start()

    # create the inference engine
    inference_engine = engine_cls(model, **config['engine_params'])

    n = 0
    iterator = dataloader if rank != 0 else tqdm(dataloader, total=len(dataloader))
    step = get_world_size()
    total_len = shape[axis]
    for batch in iterator:
        image = batch['image'].to(f'cuda:{gpu}', non_blocking=True)
        h, w = batch['size']
        image = factor_pad(image, config['padding_factor'])

        output = inference_engine.infer(image)
        instance_cells = inference_engine.get_instance_cells(
            output['ctr_hmp'], output['offsets'], upsampling
        )

        # correctly resize the sem and instance_cells
        coarse_sem_logits = output['sem_logits']
        sem_logits = coarse_sem_logits.clone()
        features = output['semantic_x']
        sem, _ = inference_engine.upsample_logits(
            sem_logits, coarse_sem_logits,
            features, upsampling * 4
        )

        # get median semantic seg
        sems = all_gather(sem)
        instance_cells = all_gather(instance_cells)

        # drop last segs if unnecessary
        n += len(sems)
        stop = min(step, (total_len - n) + step)

        # clip off extras
        sems = sems[:stop]
        instance_cells = instance_cells[:stop]

        if rank == 0:
            # run the matching process
            for sem, cells in zip(sems, instance_cells):
                queue.put(
                    (sem.cpu()[..., :h, :w], cells.cpu()[..., :h, :w])
                )

        del sems, instance_cells

    # pass None to queue to mark the end of inference
    if rank == 0:
        queue.put(('finish', 'finish'))
        rle_stack = matcher_out.recv()[0]
        matcher_proc.join()

        print('Finished matcher')

        # send the rle stack back to the main process
        rle_out.put([rle_stack])

class MultiGPUEngine3d:
    def __init__(
        self,
        model_config,
        inference_scale=1,
        label_divisor=1000,
        median_kernel_size=5,
        stuff_area=64,
        void_label=0,
        nms_threshold=0.1,
        nms_kernel=3,
        confidence_thr=0.3,
        force_connected=True,
        min_size=500,
        min_extent=4,
        fine_boundaries=False,
        semantic_only=False,
        store_url=None,
        save_panoptic=False
    ):
        # check whether GPU is available
        if not torch.cuda.device_count() > 1:
            raise Exception(f'MultiGPU inference requires multiple GPUs! Run torch.cuda.device_count()')

        # load the base and render models
        self.labels = model_config['labels']
        self.config = model_config
        self.config['model_url'] = model_config['model']

        self.config['engine_params'] = {}
        if semantic_only:
            self.config['engine_params']['thing_list'] = []
        else:
            self.config['engine_params']['thing_list'] = self.config['thing_list']

        self.config['inference_scale'] = inference_scale
        self.config['engine_params']['label_divisor'] = label_divisor
        self.config['engine_params']['median_kernel_size'] = median_kernel_size
        self.config['engine_params']['stuff_area'] = stuff_area
        self.config['engine_params']['void_label'] = void_label
        self.config['engine_params']['nms_threshold'] = nms_threshold
        self.config['engine_params']['nms_kernel'] = nms_kernel
        self.config['engine_params']['confidence_thr'] = confidence_thr
        self.config['engine_params']['coarse_boundaries'] = not fine_boundaries

        self.axes = {'xy': 0, 'xz': 1, 'yz': 2}
        self.config['axes'] = self.axes
        self.config['matcher_params'] = {}

        self.config['matcher_params']['label_divisor'] = label_divisor
        self.config['matcher_params']['merge_iou_thr'] = 0.25
        self.config['matcher_params']['merge_ioa_thr'] = 0.25
        self.config['force_connected'] = force_connected

        self.config['world_size'] = torch.cuda.device_count()

        self.min_size = min_size
        self.min_extent = min_extent

        self.save_panoptic = save_panoptic
        if store_url is not None:
            self.zarr_store = zarr.open(store_url, mode='w')
        else:
            self.zarr_store = None

        self.set_dtype()

    def set_dtype(self):
        # maximum possible value in panoptic seg
        max_index = self.config['matcher_params']['label_divisor'] * (1 + max(self.labels))
        if max_index < 2 ** 8:
            self.dtype = np.uint8
        elif max_index < 2 ** 16:
            self.dtype = np.uint16
        elif max_index < 2 ** 32:
            self.dtype = np.uint32
        else:
            self.dtype = np.uint64

    def create_matchers(self):
        matchers = [
            RLEMatcher(thing_class, **self.config['matcher_params'])
            for thing_class in self.config['thing_list']
        ]
        return matchers

    def create_trackers(self, shape3d, axis_name):
        label_divisor = self.config['engine_params']['label_divisor']
        trackers = [
            InstanceTracker(label, label_divisor, shape3d, axis_name)
            for label in self.labels
        ]
        return trackers

    def create_panoptic_stack(self, axis_name, shape3d):
        # faster IO with chunking only along
        # the given axis, orthogonal viewing is slow though
        if self.zarr_store is not None and self.save_panoptic:
            chunks = [None, None, None]
            chunks[self.axes[axis_name]] = 1
            stack = self.zarr_store.create_dataset(
                f'panoptic_{axis_name}', shape=shape3d,
                dtype=self.dtype, chunks=tuple(chunks), overwrite=True
            )
        elif self.save_panoptic:
            # we'll use uint32 for in memory segs
            stack = np.zeros(shape3d, dtype=self.dtype)
        else:
            stack = None

        return stack

    def infer_on_axis(self, volume, axis_name):
        ctx = mp.get_context('spawn')

        # create a pipe to get rle stack from main GPU process
        rle_out = ctx.Queue()

        # launch the GPU processes
        rle_stack = []
        context = mp.spawn(
            main_worker, nprocs=self.config['world_size'],
            args=(volume, axis_name, rle_stack, rle_out, self.config),
            join=False
        )

        # grab the zarr stack that was filled in
        rle_stack = rle_out.get()[0]
        context.join()

        # run backward matching and tracking
        print('Propagating labels backward...')
        axis = self.config['axes'][axis_name]
        matchers = self.create_matchers()
        trackers = self.create_trackers(volume.shape, axis_name)
        stack = self.create_panoptic_stack(axis_name, volume.shape)

        # no new labels in backward pass
        for matcher in matchers:
            matcher.assign_new = False

        rev_indices = np.arange(0, volume.shape[axis])[::-1]
        for rev_idx in tqdm(rev_indices):
            rev_idx = rev_idx.item()
            rle_seg = rle_stack[rev_idx]

            for matcher in matchers:
                class_id = matcher.class_id
                if matcher.target_rle is None:
                    matcher.initialize_target(rle_seg[class_id])
                else:
                    rle_seg[class_id] = matcher(rle_seg[class_id])

            # store the panoptic seg if desired
            if stack is not None:
                shape2d = tuple([s for i,s in enumerate(volume.shape) if i != axis])
                pan_seg = rle_seg_to_pan_seg(rle_seg, shape2d)
                put(stack, rev_idx, pan_seg, axis)

            # track each instance for each class
            for tracker in trackers:
                class_id = tracker.class_id
                tracker.update(rle_seg[class_id], rev_idx)

        # finish tracking
        for tracker in trackers:
            tracker.finish()

            # apply filters
            filters.remove_small_objects(tracker, min_size=self.min_size)
            filters.remove_pancakes(tracker, min_span=self.min_extent)

        return stack, trackers
