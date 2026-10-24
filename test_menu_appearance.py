"""Synthetic selection fixtures; these are not production SmartFX captures."""
import base64
import unittest
from unittest import mock

import airtest_backend as backend


class MenuAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv, cls.np, _ = backend._load()

    def png(self, frame):
        return base64.b64encode(self.cv.imencode('.png', frame)[1]).decode()

    def menu(self, *, selected=None, light=False, labels=('Y', 'N', 'P'), ink=20,
             scrollbar=False, dots=False):
        frame = self.np.full((len(labels)*33, 269, 3), 255, self.np.uint8)
        edge = 254 if scrollbar else 269
        if selected is not None:
            color = (250, 225, 170) if light else (215, 105, 35)
            frame[selected*33:(selected+1)*33, :edge] = color
        for row, label in enumerate(labels):
            value = (255 if selected == row and not light and ink == 20 else ink)
            self.cv.putText(frame, label, (12, row*33+23), self.cv.FONT_HERSHEY_SIMPLEX,
                            .6, (value, value, value), 1, self.cv.LINE_AA)
        # Real list context includes row separators and frame edges. Preserve
        # those pixels rather than weakening the low-information rejection.
        for y in range(32, len(frame), 33):
            frame[y] = (180, 180, 180)
        frame[:, 0] = (180, 180, 180)
        frame[:, -1] = (180, 180, 180)
        if scrollbar:
            frame[:, 220] = (180, 180, 180)
            frame[:, edge:] = (235, 235, 235)
            frame[45:74, edge+3:edge+12] = (160, 160, 160)
        if selected is not None and light:
            self.cv.rectangle(frame, (0, selected*33), (edge-1, (selected+1)*33-1), (215,155,95), 1)
        if dots and selected is not None:
            # Focus rectangle lies immediately outside the solid selection.
            for y in (selected*33-1, (selected+1)*33-1):
                if 0 <= y < len(frame):
                    frame[y, :edge] = 255
                    frame[y, :edge:2] = (215, 105, 35)
        return frame

    def match(self, pattern, current, *, row=1, duplicate=False):
        h, w = pattern.shape[:2]
        screenshot = current
        if duplicate:
            screenshot = self.np.concatenate((current, self.np.full((10,w,3),255,self.np.uint8), current))
        target = {'format': 'computer-image-target/v1', 'template_png': self.png(pattern),
                  'width': w, 'height': h, 'anchor': {'x':112/w, 'y':(row*33+17)/h},
                  'capture_window': {'width':w, 'height':len(screenshot)},
                  'min_score':.94, 'ambiguity_margin':.03}
        return backend.match_candidates(target, self.png(screenshot), allow_selection_normalization=True)

    def test_selected_N_matches_idle_at_original_location(self):
        result = self.match(self.menu(), self.menu(selected=1))
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['rect'], {'x':0,'y':0,'width':269,'height':99})
        self.assertTrue(result['appearance_verified'])
        self.assertLess(result['original_score'], .94)

    def test_selected_P_is_not_accepted_as_N(self):
        self.assertNotEqual(self.match(self.menu(), self.menu(selected=1, labels=('Y','P','P')))['status'], 'matched')

    def test_selected_Hybrid_preserves_grid_and_scrollbar(self):
        labels = ('Family', 'Hybrid', 'Standard', 'Other', 'All')
        result = self.match(self.menu(labels=labels,scrollbar=True),
                            self.menu(selected=1,labels=labels,scrollbar=True))
        self.assertEqual(result['status'], 'matched')
        changed = self.menu(selected=1, labels=labels, scrollbar=True)
        changed[45:74, 257:266] = 235
        changed[90:119, 257:266] = 160
        self.assertNotEqual(self.match(self.menu(labels=labels,scrollbar=True), changed)['status'], 'matched')

    def test_active_sheet_light_fill_and_joined_border(self):
        labels = ('Active sheet', 'Formula', 'Grouping')
        self.assertEqual(self.match(self.menu(labels=labels), self.menu(selected=0,light=True,labels=labels), row=0)['status'], 'matched')

    def test_focus_dots_and_one_gray_level_keep_same_glyph(self):
        current = self.menu(selected=1, dots=True)
        pattern = self.menu()
        pattern[32] = 255
        pattern[65] = 255
        self.assertEqual(self.match(pattern, current)['status'], 'matched')
        a = self.menu(labels=('Export','Export','Export'))
        b = self.menu(labels=('Export','Export','Export'), ink=21)
        self.assertTrue(backend._ink_agrees(a, b, .5))

    def test_added_decimal_and_removed_label_are_rejected(self):
        base = self.menu(labels=('All','1.0','Other'))
        for labels in (('All','1.00','Other'), ('All','1.0.','Other'), ('All','','Other')):
            with self.subTest(labels=labels):
                self.assertNotEqual(self.match(base,self.menu(selected=1,labels=labels))['status'],'matched')

    def test_new_ink_in_selected_row_is_rejected(self):
        current = self.menu(selected=1)
        current[44:47, 42:45] = 255
        self.assertNotEqual(self.match(self.menu(),current)['status'],'matched')

    def test_disabled_label_tooltip_and_duplicate_are_rejected(self):
        occluded = self.menu(selected=1)
        occluded[40:60, 8:48] = 230
        for label, current in (('disabled', self.menu(selected=1,ink=160)), ('tooltip', occluded)):
            with self.subTest(label=label):
                self.assertNotEqual(self.match(self.menu(),current)['status'],'matched')
        self.assertEqual(self.match(self.menu(),self.menu(selected=1),duplicate=True)['status'],'ambiguous')

    def test_blue_meaningful_icon_or_text_cannot_change(self):
        base = self.menu()
        self.cv.rectangle(base, (90,75), (100,85), (180,60,20), -1)
        current = self.menu(selected=1)
        self.cv.rectangle(current, (90,75), (100,85), (20,60,180), -1)
        self.assertFalse(backend._ink_agrees(base,current,.5,allow_selection_normalization=True))
        self.assertNotEqual(self.match(base,current)['status'],'matched')

    def test_uncropped_blue_status_banner_is_not_a_selection(self):
        base = self.menu(labels=('State',))
        current = self.menu(selected=0,labels=('State',))
        self.assertFalse(backend._ink_agrees(base,current,.5,allow_selection_normalization=True))
        self.assertNotEqual(self.match(base,current,row=0)['status'],'matched')

    def test_selection_elsewhere_does_not_relax_anchor_text(self):
        # The matcher may normalize another row, but cannot use its selection
        # color to relax the contrast test for the clicked unselected row.
        base = self.menu(labels=('Other','Export','Other'))
        current = self.menu(selected=0,labels=('Other','Export','Other'))
        current[33:66] = self.menu(labels=('Export',),ink=90)
        self.assertFalse(backend._ink_agrees(base,current,.5,allow_selection_normalization=True))

    def test_dark_selection_is_not_available_without_private_list_proof(self):
        pattern, current = self.menu(), self.menu(selected=1)
        target = {'format':'computer-image-target/v1','template_png':self.png(pattern),
                  'width':269,'height':99,'anchor':{'x':.4,'y':.5},
                  'capture_window':{'width':269,'height':99},'min_score':.94,'ambiguity_margin':.03}
        self.assertNotEqual(backend.match_candidates(target,self.png(current))['status'],'matched')
        self.assertEqual(backend.match_candidates(target,self.png(current),allow_selection_normalization=True)['status'],'matched')

    def test_ambiguous_and_low_detail_do_not_enter_appearance_fallback(self):
        for answer in ({'status':'ambiguous'}, {'status':'not_found','code':'template_low_detail'}):
            with self.subTest(answer=answer), mock.patch.object(backend,'_rgb_candidates',return_value=answer), \
                    mock.patch.object(backend,'_appearance_png',side_effect=AssertionError('must not normalize')):
                self.assertEqual(backend.match_candidates({},''),answer)


if __name__ == '__main__':
    unittest.main()
