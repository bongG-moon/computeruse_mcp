import base64
import unittest
import airtest_backend as backend


class HoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv, cls.np, _ = backend._load()

    def png(self, value):
        return base64.b64encode(self.cv.imencode('.png', value)[1]).decode()

    def image(self, text='Export', background=255, ink=20):
        image = self.np.full((32,160,3), background, self.np.uint8)
        self.cv.putText(image,text,(8,23),self.cv.FONT_HERSHEY_SIMPLEX,.6,(ink,ink,ink),1)
        return image

    def match(self, current, duplicate=False):
        pattern = self.image()
        screen = self.np.full((96,160,3), 255, self.np.uint8)
        screen[0:32] = current
        if duplicate: screen[64:96] = current
        target = {'format':'computer-image-target/v1','template_png':self.png(pattern),
                  'width':160,'height':32,'anchor':{'x':.4,'y':.5},
                  'capture_window':{'width':160,'height':96},'min_score':.94,'ambiguity_margin':.03}
        return backend.match_candidates(target,self.png(screen))

    def test_light_hover_background_retains_text_and_threshold(self):
        result = self.match(self.image(background=(240,220,200)))
        self.assertEqual(result['status'],'matched')
        self.assertTrue(result['appearance_verified'])

    def test_changed_text_disabled_occluded_and_duplicate_are_not_clicked(self):
        occluded = self.image(background=(240,220,200))
        occluded[:,10:70] = 255
        for image in (self.image('Import', (240,220,200)), self.image(background=(240,220,200),ink=160), occluded):
            with self.subTest(case=str(image[0,0])):
                self.assertNotEqual(self.match(image)['status'],'matched')
        self.assertEqual(self.match(self.image(background=(240,220,200)),duplicate=True)['status'],'ambiguous')

    def test_light_hover_fill_and_blue_border_without_lowering_threshold(self):
        hovered = self.image(background=(240,220,200))
        self.cv.rectangle(hovered,(0,0),(159,31),(215,155,95),1)
        result = self.match(hovered)
        self.assertEqual(result['status'],'matched')
        self.assertTrue(result['appearance_verified'])

    def test_blue_interior_content_cannot_be_erased_as_border(self):
        hovered = self.image(background=(240,220,200))
        self.cv.putText(hovered,'X',(110,23),self.cv.FONT_HERSHEY_SIMPLEX,.6,(215,155,95),1)
        self.assertNotEqual(self.match(hovered)['status'],'matched')

    def test_blue_interior_rectangle_is_content_not_a_menu_border(self):
        hovered = self.image(background=(240,220,200))
        self.cv.rectangle(hovered,(110,8),(153,25),(215,155,95),1)
        self.assertNotEqual(self.match(hovered)['status'],'matched')

    def test_small_added_glyph_is_not_hidden_by_average_ink_score(self):
        self.assertNotEqual(self.match(self.image('Export.',(240,220,200)))['status'],'matched')

    def test_dark_selection_is_not_silently_normalized(self):
        self.assertNotEqual(self.match(self.image(background=(150,70,20),ink=255))['status'],'matched')


if __name__ == '__main__': unittest.main()
