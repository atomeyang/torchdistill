import sys
from collections import abc

import torch
from torch import nn

from models.special import SpecialModule, get_special_module
from myutils.common.file_util import make_parent_dirs
from myutils.pytorch.func_util import get_optimizer, get_scheduler
from myutils.pytorch.module_util import check_if_wrapped, get_module, freeze_module_params, unfreeze_module_params
from tools.loss import KDLoss, get_single_loss, get_custom_loss
from utils.dataset_util import build_data_loaders
from utils.model_util import redesign_model, wrap_model

try:
    from apex import amp
except ImportError:
    amp = None


def extract_module(org_model, sub_model, module_path):
    if module_path.startswith('+'):
        return get_module(sub_model, module_path[1:])
    return get_module(org_model, module_path)


def set_distillation_box_info(info_dict, module_path, **kwargs):
    info_dict[module_path] = kwargs


def register_forward_hook_with_dict(module, module_path, requires_input, requires_output, info_dict):
    def forward_hook4input(self, func_input, func_output):
        if isinstance(func_input, tuple) and len(func_input) == 1:
            func_input = func_input[0]
        info_dict[module_path]['input'] = func_input

    def forward_hook4output(self, func_input, func_output):
        info_dict[module_path]['output'] = func_output

    def forward_hook4io(self, func_input, func_output):
        if isinstance(func_input, tuple) and len(func_input) == 1:
            func_input = func_input[0]
        info_dict[module_path]['input'] = func_input
        info_dict[module_path]['output'] = func_output

    if requires_input and not requires_output:
        return module.register_forward_hook(forward_hook4input)
    elif not requires_input and requires_output:
        return module.register_forward_hook(forward_hook4output)
    elif requires_input and requires_output:
        return module.register_forward_hook(forward_hook4io)
    raise ValueError('Either requires_input or requires_output should be True')


def set_hooks(model, unwrapped_org_model, model_config, info_dict):
    pair_list = list()
    forward_hook_config = model_config.get('forward_hook', dict())
    if len(forward_hook_config) == 0:
        return pair_list

    input_module_path_set = set(forward_hook_config.get('input', list()))
    output_module_path_set = set(forward_hook_config.get('output', list()))
    for target_module_path in input_module_path_set.union(output_module_path_set):
        requires_input = target_module_path in input_module_path_set
        requires_output = target_module_path in output_module_path_set
        set_distillation_box_info(info_dict, target_module_path)
        target_module = extract_module(unwrapped_org_model, model, target_module_path)
        handle = register_forward_hook_with_dict(target_module, target_module_path,
                                                 requires_input, requires_output, info_dict)
        pair_list.append((target_module_path, handle))
    return pair_list


def change_device(data, device):
    elem_type = type(data)
    if isinstance(data, torch.Tensor):
        return data.to(device)
    elif isinstance(data, tuple) and hasattr(data, '_fields'):  # namedtuple
        return elem_type(*(change_device(samples, device) for samples in zip(*data)))
    elif isinstance(data, (list, tuple)):
        return elem_type(*(change_device(d, device) for d in data))
    elif isinstance(data, abc.Mapping):
        return {key: change_device(data[key], device) for key in data}
    elif isinstance(data, abc.Sequence):
        transposed = zip(*data)
        return [change_device(samples, device) for samples in transposed]
    return data


def extract_outputs(model_info_dict):
    model_output_dict = dict()
    for module_path, model_io_dict in model_info_dict.items():
        sub_model_io_dict = dict()
        for key in list(model_io_dict.keys()):
            sub_model_io_dict[key] = model_io_dict.pop(key)
        model_output_dict[module_path] = sub_model_io_dict
    return model_output_dict


class DistillationBox(nn.Module):
    def setup_data_loaders(self, train_config):
        train_data_loader_config = train_config.get('train_data_loader', dict())
        train_data_loader_config['cacheable'] = True
        val_data_loader_config = train_config.get('val_data_loader', dict())
        train_data_loader, val_data_loader =\
            build_data_loaders(self.dataset_dict, [train_data_loader_config, val_data_loader_config], self.distributed)
        if train_data_loader is not None:
            self.train_data_loader = train_data_loader
        if val_data_loader is not None:
            self.val_data_loader = val_data_loader

    def setup_teacher_student_models(self, teacher_config, student_config):
        unwrapped_org_teacher_model =\
            self.org_teacher_model.module if check_if_wrapped(self.org_teacher_model) else self.org_teacher_model
        unwrapped_org_student_model = \
            self.org_student_model.module if check_if_wrapped(self.org_student_model) else self.org_student_model
        self.target_teacher_pairs.clear()
        self.target_student_pairs.clear()
        teacher_ref_model = unwrapped_org_teacher_model
        student_ref_model = unwrapped_org_student_model
        if len(teacher_config) > 0 or (len(teacher_config) == 0 and self.teacher_model is None):
            special_teacher_model_config = teacher_config.get('special', dict())
            special_teacher_model_type = special_teacher_model_config.get('type', None)
            if special_teacher_model_type is not None:
                special_teacher_model_params_config = special_teacher_model_config.get('params', None)
                if special_teacher_model_params_config is None:
                    special_teacher_model_params_config = dict()
                special_teacher_model = get_special_module(special_teacher_model_type,
                                                           teacher_model=unwrapped_org_teacher_model,
                                                           **special_teacher_model_params_config)
                if special_teacher_model is not None:
                    teacher_ref_model = special_teacher_model
            self.teacher_model = redesign_model(teacher_ref_model, teacher_config, 'teacher')

        if len(student_config) > 0 or (len(student_config) == 0 and self.student_model is None):
            special_student_model_config = student_config.get('special', dict())
            special_student_model_type = special_student_model_config.get('type', None)
            if special_student_model_type is not None:
                special_student_model_params_config = special_student_model_config.get('params', None)
                if special_student_model_params_config is None:
                    special_student_model_params_config = dict()
                special_student_model = get_special_module(special_student_model_type,
                                                           student_model=unwrapped_org_student_model,
                                                           **special_student_model_params_config)
                if special_student_model is not None:
                    student_ref_model = special_student_model
            self.student_model = redesign_model(student_ref_model, student_config, 'student')

        self.target_teacher_pairs.extend(set_hooks(self.teacher_model, teacher_ref_model,
                                                   teacher_config, self.teacher_info_dict))
        self.target_student_pairs.extend(set_hooks(self.student_model, student_ref_model,
                                                   student_config, self.student_info_dict))

    def setup_loss(self, train_config):
        criterion_config = train_config['criterion']
        org_term_config = criterion_config.get('org_term', dict())
        org_criterion_config = org_term_config.get('criterion', dict()) if isinstance(org_term_config, dict) else None
        self.org_criterion = None if org_criterion_config is None or len(org_criterion_config) == 0 \
            else get_single_loss(org_criterion_config)
        self.criterion = get_custom_loss(criterion_config)
        self.uses_teacher_output = self.org_criterion is not None and isinstance(self.org_criterion, KDLoss)

    def setup(self, train_config):
        # Set up train and val data loaders
        self.setup_data_loaders(train_config)

        # Define teacher and student models used in this stage
        teacher_config = train_config.get('teacher', dict())
        student_config = train_config.get('student', dict())
        self.setup_teacher_student_models(teacher_config, student_config)

        # Define loss function used in this stage
        self.setup_loss(train_config)

        # Wrap models if necessary
        self.teacher_model =\
            wrap_model(self.teacher_model, teacher_config, self.device, self.device_ids, self.distributed)
        self.student_model =\
            wrap_model(self.student_model, student_config, self.device, self.device_ids, self.distributed)
        if not teacher_config.get('requires_grad', True):
            print('Freezing the whole teacher model')
            freeze_module_params(self.teacher_model)

        if not student_config.get('requires_grad', True):
            print('Freezing the whole student model')
            freeze_module_params(self.student_model)

        # Set up optimizer and scheduler
        optim_config = train_config.get('optimizer', dict())
        optimizer_reset = False
        if len(optim_config) > 0:
            self.optimizer = get_optimizer(self.student_model, optim_config['type'], optim_config['params'])
            optimizer_reset = True

        scheduler_config = train_config.get('scheduler', None)
        if scheduler_config is not None and len(scheduler_config) > 0:
            self.lr_scheduler = get_scheduler(self.optimizer, scheduler_config['type'], scheduler_config['params'])
        elif optimizer_reset:
            self.lr_scheduler = None

        # Set up apex if you require mixed-precision training
        self.apex = False
        apex_config = train_config.get('apex', None)
        if apex_config is not None and apex_config.get('requires', False):
            if sys.version_info < (3, 0):
                raise RuntimeError('Apex currently only supports Python 3. Aborting.')
            if amp is None:
                raise RuntimeError('Failed to import apex. Please install apex from https://www.github.com/nvidia/apex '
                                   'to enable mixed-precision training.')
            self.student_model, self.optimizer =\
                amp.initialize(self.student_model, self.optimizer, opt_level=apex_config['opt_level'])
            self.apex = True

    def __init__(self, teacher_model, student_model, dataset_dict, train_config, device, device_ids, distributed):
        super().__init__()
        self.org_teacher_model = teacher_model
        self.org_student_model = student_model
        self.dataset_dict = dataset_dict
        self.device = device
        self.device_ids = device_ids
        self.distributed = distributed
        self.teacher_model = None
        self.student_model = None
        self.target_teacher_pairs, self.target_student_pairs = list(), list()
        self.teacher_info_dict, self.student_info_dict = dict(), dict()
        self.train_data_loader, self.val_data_loader, self.optimizer, self.lr_scheduler = None, None, None, None
        self.org_criterion, self.criterion, self.uses_teacher_output = None, None, None
        self.apex = None
        self.setup(train_config)
        self.num_epochs = train_config['num_epochs']

    def pre_process(self, epoch=None, **kwargs):
        self.teacher_model.eval()
        self.student_model.train()
        if self.distributed and self.train_data_loader.sampler is not None:
            self.train_data_loader.sampler.set_epoch(epoch)

    def check_if_org_loss_required(self):
        return self.org_criterion is not None

    def get_teacher_output(self, sample_batch, cached_data, cache_file_paths):
        if cached_data is not None and isinstance(cached_data, dict):
            device = sample_batch.device
            teacher_outputs = cached_data.get('teacher_outputs', None)
            extracted_teacher_output_dict = cached_data['extracted_outputs']
            if device.type != 'cpu':
                teacher_outputs = change_device(teacher_outputs, device)
                extracted_teacher_output_dict = change_device(extracted_teacher_output_dict, device)
            return teacher_outputs, extracted_teacher_output_dict

        teacher_outputs = self.teacher_model(sample_batch)
        if isinstance(self.teacher_model, SpecialModule):
            self.teacher_model.post_forward(self.teacher_info_dict)

        extracted_teacher_output_dict = extract_outputs(self.teacher_info_dict)
        if isinstance(cached_data, (list, tuple)) \
                and isinstance(cache_file_paths, (list, tuple)) and cache_file_paths[0] != '':
            device = sample_batch.device
            cpu_device = torch.device('cpu')
            for i, (teacher_output, cache_file_path) in enumerate(zip(teacher_outputs.cpu(), cache_file_paths)):
                sub_dict = dict()
                for key, value in extracted_teacher_output_dict.items():
                    sub_dict[key] = value[i]

                if device.type != 'cpu':
                    sub_dict = change_device(sub_dict, cpu_device)

                cache_dict = {'teacher_outputs': teacher_output, 'extracted_outputs': sub_dict}
                make_parent_dirs(cache_file_path)
                torch.save(cache_dict, cache_file_path)
        return teacher_outputs, extracted_teacher_output_dict

    def forward(self, sample_batch, targets, cached_data=None, cache_file_paths=None):
        teacher_outputs, extracted_teacher_output_dict =\
            self.get_teacher_output(sample_batch, cached_data=cached_data, cache_file_paths=cache_file_paths)
        student_outputs = self.student_model(sample_batch)
        if isinstance(self.student_model, SpecialModule):
            self.student_model.post_forward(self.student_info_dict)

        org_loss_dict = dict()
        if self.check_if_org_loss_required():
            # Models with auxiliary classifier returns multiple outputs
            if isinstance(student_outputs, (list, tuple)):
                if self.uses_teacher_output:
                    for i, sub_student_outputs, sub_teacher_outputs in enumerate(zip(student_outputs, teacher_outputs)):
                        org_loss_dict[i] = self.org_criterion(sub_student_outputs, sub_teacher_outputs, targets)
                else:
                    for i, sub_outputs in enumerate(student_outputs):
                        org_loss_dict[i] = self.org_criterion(sub_outputs, targets)
            else:
                org_loss = self.org_criterion(student_outputs, teacher_outputs, targets) if self.uses_teacher_output\
                    else self.org_criterion(student_outputs, targets)
                org_loss_dict = {0: org_loss}

        output_dict = {'teacher': extracted_teacher_output_dict,
                       'student': extract_outputs(self.student_info_dict)}
        total_loss = self.criterion(output_dict, org_loss_dict)
        return total_loss

    def update_params(self, loss):
        self.optimizer.zero_grad()
        if self.apex:
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        self.optimizer.step()

    def post_process(self, **kwargs):
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
        if isinstance(self.teacher_model, SpecialModule):
            self.teacher_model.post_process()
        if isinstance(self.student_model, SpecialModule):
            self.student_model.post_process()

    def clean_modules(self):
        unfreeze_module_params(self.org_teacher_model)
        unfreeze_module_params(self.org_student_model)
        self.teacher_info_dict.clear()
        self.student_info_dict.clear()
        for _, module_handle in self.target_teacher_pairs + self.target_student_pairs:
            module_handle.remove()


class MultiStagesDistillationBox(DistillationBox):
    def __init__(self, teacher_model, student_model, data_loader_dict, train_config, device, device_ids, distributed):
        stage1_config = train_config['stage1']
        super().__init__(teacher_model, student_model, data_loader_dict, stage1_config, device, device_ids, distributed)
        self.train_config = train_config
        self.stage_number = 1
        self.stage_end_epoch = stage1_config['num_epochs']
        self.num_epochs = sum(train_config[key]['num_epochs'] for key in train_config.keys() if key.startswith('stage'))
        self.current_epoch = 0
        print('Started stage {}'.format(self.stage_number))

    def advance_to_next_stage(self):
        self.clean_modules()
        self.stage_number += 1
        next_stage_config = self.train_config['stage{}'.format(self.stage_number)]
        self.setup(next_stage_config)
        self.stage_end_epoch += next_stage_config['num_epochs']
        print('Advanced to stage {}'.format(self.stage_number))

    def post_process(self, **kwargs):
        super().post_process()
        self.current_epoch += 1
        if self.current_epoch == self.stage_end_epoch and self.current_epoch < self.num_epochs:
            self.advance_to_next_stage()


def get_distillation_box(teacher_model, student_model, data_loader_dict, train_config, device, device_ids, distributed):
    if 'stage1' in train_config:
        return MultiStagesDistillationBox(teacher_model, student_model, data_loader_dict,
                                          train_config, device, device_ids, distributed)
    return DistillationBox(teacher_model, student_model, data_loader_dict, train_config,
                           device, device_ids, distributed)