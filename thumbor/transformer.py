#!/usr/bin/python
# -*- coding: utf-8 -*-

# thumbor imaging service
# https://github.com/globocom/thumbor/wiki

# Licensed under the MIT license:
# http://www.opensource.org/licenses/mit-license
# Copyright (c) 2011 globo.com timehome@corp.globo.com

import math

from thumbor.point import FocalPoint
from thumbor.utils import logger

trim_enabled = True
try:
    from thumbor.ext.filters import _bounding_box
except ImportError:
    logger.warn("Error importing bounding_box filter, trimming won't work")
    trim_enabled = False


class Transformer(object):
    def __init__(self, context):
        self.context = context
        self.engine = self.context.modules.engine

    def calculate_target_dimensions(self):
        source_width, source_height = self.engine.size
        source_width = float(source_width)
        source_height = float(source_height)

        if not self.context.request.width and not self.context.request.height:
            self.target_width = source_width
            self.target_height = source_height
        else:
            if self.context.request.width:
                if self.context.request.width == "orig":
                    self.target_width = source_width
                else:
                    self.target_width = float(self.context.request.width)
            else:
                self.target_width = self.engine.get_proportional_width(self.context.request.height)

            if self.context.request.height:
                if self.context.request.height == "orig":
                    self.target_height = source_height
                else:
                    self.target_height = float(self.context.request.height)
            else:
                self.target_height = self.engine.get_proportional_height(self.context.request.width)

    def adjust_focal_points(self):
        source_width, source_height = self.engine.size

        self.focal_points = None

        if self.context.request.focal_points:
            if self.context.request.should_crop:
                self.focal_points = []
                crop = self.context.request.crop
                for point in self.context.request.focal_points:
                    if point.x < crop['left'] or point.x > crop['right'] or \
                       point.y < crop['top']  or point.y > crop['bottom']:
                        continue
                    point.x -= crop['left'] or 0
                    point.y -= crop['top'] or 0
                    self.focal_points.append(point)
            else:
                self.focal_points = self.context.request.focal_points

        if not self.focal_points:
            self.focal_points = [
                FocalPoint.from_alignment(self.context.request.halign,
                                          self.context.request.valign,
                                          source_width,
                                          source_height)
            ]

        self.engine.focus(self.focal_points)

    def transform(self, callback):
        self.done_callback = callback
        self.reorientate()
        self.trim()
        self.smart_detect()

    def trim(self):
        if self.context.request.trim is None or not trim_enabled:
            return
        box = _bounding_box.apply(self.engine.get_image_mode(), self.engine.size[0], self.engine.size[1], self.context.request.trim_pos, self.context.request.trim_tolerance, self.engine.get_image_data())
        if box[2] < box[0] or box[3] < box[1]:
            logger.warn("Ignoring trim, there wouldn't be any image left, check the tolerance.")
            return

        self.engine.crop(box[0], box[1], box[2] + 1, box[3] + 1)
        if self.context.request.should_crop:
            self.context.request.crop['left'] -= box[0]
            self.context.request.crop['top'] -= box[1]
            self.context.request.crop['right'] -= box[0]
            self.context.request.crop['bottom'] -= box[1]

    def reorientate(self):
        if self.context.config.RESPECT_ORIENTATION:
            engine = self.context.modules.engine
            if hasattr(engine, 'exif'):
                if 'Orientation' in engine.exif:
                    orientation = engine.exif['Orientation']
                    if orientation == 2:
                        engine.flip_horizontally()
                    elif orientation == 3:
                        engine.rotate(180)
                    elif orientation == 4:
                        engine.flip_vertically()
                    elif orientation == 5:
                        # Horizontal Mirror + Rotation 270
                        engine.flip_vertically()
                        engine.rotate(270)
                    elif orientation == 6:
                        engine.rotate(270)
                    elif orientation == 7:
                        # Vertical Mirror + Rotation 270
                        engine.flip_horizontally()
                        engine.rotate(270)
                    elif orientation == 8:
                        engine.rotate(90)

    @property
    def smart_storage_key(self):
        return self.context.request.image_url

    def smart_detect(self):
        if self.context.modules.detectors and self.context.request.smart:
            storage = self.context.modules.storage
            focal_points = storage.get_detector_data(self.smart_storage_key)
            if focal_points is not None:
                self.after_smart_detect(focal_points, points_from_storage=True)
            else:
                detectors = self.context.modules.detectors
                detectors[0](self.context, index=0, detectors=detectors).detect(self.after_smart_detect)
        else:
            self.after_smart_detect([])

    def after_smart_detect(self, focal_points=[], points_from_storage=False):
        self.manual_crop()
        self.calculate_target_dimensions()

        for point in focal_points:
            self.context.request.focal_points.append(FocalPoint.from_dict(point))

        if self.context.request.focal_points and self.context.modules.storage and not points_from_storage:
            storage = self.context.modules.storage
            points = []
            for point in self.context.request.focal_points:
                points.append(point.to_dict())

            storage.put_detector_data(self.smart_storage_key, points)

        self.adjust_focal_points()

        if self.context.request.debug:
            self.debug()
        else:
            if self.context.request.fit_in:
                self.fit_in_resize()
            else:
                self.auto_crop()
                self.resize()
            self.flip()

        self.done_callback()

    def manual_crop(self):
        if self.context.request.should_crop:
            limit = lambda dimension, maximum: min(max(dimension, 0), maximum)

            source_width, source_height = self.engine.size
            crop = self.context.request.crop

            crop['left'] = limit(crop['left'], source_width)
            crop['top'] = limit(crop['top'], source_height)
            crop['right'] = limit(crop['right'], source_width)
            crop['bottom'] = limit(crop['bottom'], source_height)

            if crop['left'] >= crop['right'] or crop['top'] >= crop['bottom']:
                self.context.request.should_crop = False
                crop['left'] = crop['right'] = crop['top'] = crop['bottom'] = 0
                return

            self.engine.crop(crop['left'], crop['top'], crop['right'], crop['bottom'])

    def auto_crop(self):
        source_width, source_height = self.engine.size

        source_ratio = round(float(source_width) / source_height, 2)
        target_ratio = round(float(self.target_width) / self.target_height, 2)

        if source_ratio == target_ratio:
            return

        focal_x, focal_y = self.get_center_of_mass()

        if self.target_width / source_width > self.target_height / source_height:
            crop_width = source_width
            crop_height = round(source_width * self.target_height / self.target_width, 0)
        else:
            crop_width = round(math.ceil(self.target_width * source_height / self.target_height), 0)
            crop_height = source_height

        crop_left = min(max(focal_x - (crop_width / 2), 0.0), source_width - crop_width)
        crop_right = min(crop_left + crop_width, source_width)

        crop_top = min(max(focal_y - (crop_height / 2), 0.0), source_height - crop_height)
        crop_bottom = min(crop_top + crop_height, source_height)

        self.engine.crop(crop_left, crop_top, crop_right, crop_bottom)

    def flip(self):
        if self.context.request.horizontal_flip:
            self.engine.flip_horizontally()
        if self.context.request.vertical_flip:
            self.engine.flip_vertically()

    def get_center_of_mass(self):
        total_weight = 0.0
        total_x = 0.0
        total_y = 0.0

        for focal_point in self.focal_points:
            total_weight += focal_point.weight

            total_x += focal_point.x * focal_point.weight
            total_y += focal_point.y * focal_point.weight

        x = total_x / total_weight
        y = total_y / total_weight

        return x, y

    def resize(self):
        source_width, source_height = self.engine.size
        if self.target_width == source_width and self.target_height == source_height:
            return
        self.engine.resize(self.target_width, self.target_height)

    def fit_in_resize(self):
        source_width, source_height = self.engine.size

        #invert width and height if image orientation is not the same as request orientation and need adaptive
        if self.context.request.adaptive and ((source_width - source_height < 0 and self.target_width - self.target_height > 0 ) or (source_width - source_height > 0 and self.target_width - self.target_height < 0 )):
            tmp = self.context.request.width
            self.context.request.width = self.context.request.height
            self.context.request.height = tmp
            tmp = self.target_width
            self.target_width = self.target_height
            self.target_height = tmp

        if self.target_width >= source_width and self.target_height >= source_height:
            return

        if source_width / self.target_width >= source_height / self.target_height:
            resize_height = round(source_height * self.target_width / source_width)
            resize_width = self.target_width
        else:
            resize_height = self.target_height
            resize_width = round(source_width * self.target_height / source_height)

        self.engine.resize(resize_width, resize_height)

    def debug(self):
        if not self.context.request.focal_points:
            return

        for point in self.context.request.focal_points:
            if point.width <= 1:
                point.width = 10
            if point.height <= 1:
                point.height = 10
            self.engine.draw_rectangle(int(point.x - (point.width / 2)),
                                       int(point.y - (point.height / 2)),
                                       point.width,
                                       point.height)

