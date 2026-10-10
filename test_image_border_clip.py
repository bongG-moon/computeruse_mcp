"""Strict source-pixel anchor handling for small full-window border differences."""
import copy
import unittest

from image_targets import validate_match
from operations import OperationError
from test_image_steps import image_target, png


class ImageBorderClipTests(unittest.TestCase):
    def setUp(self):
        self.target = {**image_target(), 'template_png': png(50, 8), 'width': 50, 'height': 8,
            'source_size': {'width': 100, 'height': 16}, 'anchor': {'x': .75, 'y': .5}}
        self.shot = png(98, 98)
        self.answer = {'status': 'matched', 'score': .99, 'candidate_count': 1, 'scale': 2,
            'rect': {'x': 0, 'y': 30, 'width': 98, 'height': 16}, 'screenshot': {'width': 98, 'height': 98},
            'template_clip': {'left': 1, 'right': 1, 'top': 0, 'bottom': 0, 'original_width': 100, 'original_height': 16}}

    def test_source_anchor_is_translated_not_rescaled_after_horizontal_border_clip(self):
        answer = validate_match(self.answer, self.target, self.shot)
        self.assertEqual((answer['x'], answer['y']), (74, 38))
        self.assertEqual(answer['template_clip'], self.answer['template_clip'])

    def test_both_axes_use_original_pixels_and_odd_border_removal_stays_centered(self):
        target = {**image_target(), 'template_png': png(50, 50), 'width': 50, 'height': 50,
            'source_size': {'width': 100, 'height': 100}, 'anchor': {'x': .25, 'y': .75}}
        answer = {**self.answer, 'rect': {'x': 0, 'y': 0, 'width': 97, 'height': 96},
            'screenshot': {'width': 97, 'height': 96},
            'template_clip': {'left': 1, 'right': 2, 'top': 2, 'bottom': 2, 'original_width': 100, 'original_height': 100}}
        result = validate_match(answer, target, png(97, 96))
        self.assertEqual((result['x'], result['y']), (24, 73))

    def test_removed_anchor_is_rejected_instead_of_clamped_into_retained_region(self):
        for anchor in (0, 1, .999):
            target = {**self.target, 'anchor': {'x': anchor, 'y': .5}}
            with self.subTest(anchor=anchor), self.assertRaises(OperationError): validate_match(self.answer, target, self.shot)

    def test_oversized_noninteger_asymmetric_unscaled_or_interior_clips_are_rejected(self):
        for changes in ({'left': True}, {'left': -1}, {'left': 5}, {'left': 0, 'right': 2},
                        {'original_width': 101}, {'original_height': 17}, {'extra': 0}):
            answer = copy.deepcopy(self.answer); answer['template_clip'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(OperationError): validate_match(answer, self.target, self.shot)
        target = {**self.target, 'capture_window': {'width': 102, 'height': 100}}
        with self.assertRaises(OperationError): validate_match(self.answer, target, self.shot)
        target = {**self.target, 'capture_window': {'width': 100, 'height': 104}}
        with self.assertRaises(OperationError): validate_match(self.answer, target, self.shot)

    def test_a_clip_cannot_override_second_candidate_ambiguity_or_lower_threshold(self):
        for changes in ({'second_score': .98}, {'candidate_count': 2}, {'second_score': float('nan')}, {'score': .93}):
            with self.subTest(changes=changes), self.assertRaises(OperationError): validate_match({**self.answer, **changes}, self.target, self.shot)
        result = validate_match({**self.answer, 'candidate_count': 2, 'second_score': .95}, self.target, self.shot)
        self.assertEqual(result['status'], 'matched')

    def test_clipping_cannot_remove_the_minimum_recognizable_region(self):
        target = {**image_target(), 'template_png': png(8, 8), 'width': 8, 'height': 8,
            'source_size': {'width': 8, 'height': 8}, 'capture_window': {'width': 8, 'height': 8}}
        answer = {**self.answer, 'rect': {'x': 0, 'y': 0, 'width': 4, 'height': 8},
            'screenshot': {'width': 4, 'height': 8},
            'template_clip': {'left': 2, 'right': 2, 'top': 0, 'bottom': 0, 'original_width': 8, 'original_height': 8}}
        with self.assertRaises(OperationError): validate_match(answer, target, png(4, 8))

    def test_nonmatched_result_never_exposes_clip_or_input_coordinates(self):
        result = validate_match({**self.answer, 'status': 'ambiguous'}, self.target, self.shot)
        self.assertNotIn('x', result)
        self.assertNotIn('template_clip', result)


if __name__ == '__main__': unittest.main()
