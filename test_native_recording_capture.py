"""Exercise target-render validation and DWM crop without opening any windows."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
PROBE = r"""
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Collections.Generic;
using System.Collections;
using System.Reflection;
using System.Web.Script.Serialization;
internal static class RenderProbe {
    static int Main(string[] args) {
        var json = new JavaScriptSerializer();
        if(args[0]=="payload_budget") {
            string pixels=new String('A',900000);var events=new List<Dictionary<string,object>>();
            for(int index=0;index<300;index++) events.Add(new Dictionary<string,object>{{"operation",index%2==0?"click":"press_key"},
                {"program_id","test"},{"window_ref","main"},{"template_png",pixels},{"key","TAB"},{"width",48},{"height",48}});
            var first=events[0];var budget=new VisualTools.RecordingPayloadBudget();bool limited=budget.Bound(events);
            int manual=0;foreach(var item in events) if(Convert.ToString(item["operation"])=="manual_entry") manual++;
            VisualTools.Write(args[1],new Dictionary<string,object>{{"events",events},{"warnings",new string[]{"recording_payload_limit"}}});
            var weird=new Dictionary<string,object>{{"quoted","한글😀 <'&>\\\"\r\n"}};
            Console.WriteLine(json.Serialize(new {limited=limited,count=events.Count,manual=manual,first_preserved=Object.ReferenceEquals(first,events[0])&&Object.ReferenceEquals(events[0]["template_png"],pixels),
                last_reason=events[299]["reason"],last_requested=events[299]["requested_operation"],last_has_pixels=events[299].ContainsKey("template_png"),last_has_key=events[299].ContainsKey("key"),
                bounded=budget.Bytes(events)<=VisualTools.RecordingPayloadBudget.MaximumBytes,written_bytes=new System.IO.FileInfo(args[1]).Length,
                escaping_counted=budget.Bytes(weird)>=System.Text.Encoding.UTF8.GetByteCount(json.Serialize(weird))}));return 0;
        }
        if(args[0]=="recording_limits") {
            Console.WriteLine(json.Serialize(new {event_min=VisualTools.ValidRecordingEventLimit(1),event_max=VisualTools.ValidRecordingEventLimit(300),
                event_zero=VisualTools.ValidRecordingEventLimit(0),event_over=VisualTools.ValidRecordingEventLimit(301),
                popup_last=VisualTools.RecordingPopupCapacity(4,299),popup_over=VisualTools.RecordingPopupCapacity(0,300),
                popup_active_over=VisualTools.RecordingPopupCapacity(5,0),editor_steps=ProcessEditor.MaximumSteps}));return 0;
        }
        if(args[0]=="editor_payload") {
            var payload=ProcessEditor.Json.Deserialize<Dictionary<string,object>>(ProcessEditor.Read(args[1],ProcessEditor.MaximumMessageBytes));
            var draft=(Dictionary<string,object>)payload["draft"];var rows=(IList)draft["steps"];
            Console.WriteLine(json.Serialize(new {count=rows.Count,last=((Dictionary<string,object>)rows[rows.Count-1])["index"]}));return 0;
        }
        if(args[0].StartsWith("async_")) {
            var recorder=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
            var pending=recorder.GetNestedType("Pending",BindingFlags.NonPublic);
            object first=Activator.CreateInstance(pending,true),second=Activator.CreateInstance(pending,true),key=Activator.CreateInstance(pending,true),other=Activator.CreateInstance(pending,true),removed=Activator.CreateInstance(pending,true);
            var owner=pending.GetField("ImageOwner",BindingFlags.Instance|BindingFlags.NonPublic);
            var saved=pending.GetField("Saved",BindingFlags.Instance|BindingFlags.NonPublic);
            var dependants=pending.GetField("ImageDependants",BindingFlags.Instance|BindingFlags.NonPublic);
            var keyEvent=new Dictionary<string,object>{{"operation","press_key"},{"key","TAB"},{"capture_issue","image_authoring_pending"}};
            var otherEvent=new Dictionary<string,object>{{"operation","press_key"},{"key","ENTER"},{"template_png","second_frame"}};
            var removedEvent=new Dictionary<string,object>{{"operation","press_key"},{"key","ENTER"},{"capture_issue","image_authoring_pending"}};
            saved.SetValue(key,keyEvent);saved.SetValue(other,otherEvent);saved.SetValue(removed,removedEvent);owner.SetValue(key,second);owner.SetValue(other,first);
            if(args[0]=="async_redirect") {
                var queueType=typeof(Queue<>).MakeGenericType(pending);var queue=Activator.CreateInstance(queueType);
                queueType.GetMethod("Enqueue").Invoke(queue,new object[]{key});queueType.GetMethod("Enqueue").Invoke(queue,new object[]{other});
                recorder.GetMethod("RedirectImageDependants",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{queue,second,first});
                Console.WriteLine(json.Serialize(new {queued_key_redirected=Object.ReferenceEquals(owner.GetValue(key),first),other_owner_preserved=Object.ReferenceEquals(owner.GetValue(other),first)}));return 0;
            }
            ((IList)dependants.GetValue(first)).Add(key);((IList)dependants.GetValue(first)).Add(removed);
            if(args[0]=="async_failure_native") keyEvent["native_target"]=new Dictionary<string,object>{{"identity","exact_owned_edit"}};
            var active=new List<Dictionary<string,object>>{keyEvent,otherEvent};
            var image=new Dictionary<string,object>{{"template_png","first_pre_input_frame"},{"width",48},{"operation","click"},{"key","WRONG"},{"value","WRONG"}};
            recorder.GetMethod("CompleteImageDependants",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{first,args[0].StartsWith("async_failure")?null:image,"image_ambiguous",active});
            Console.WriteLine(json.Serialize(new {key=keyEvent,other=otherEvent,removed=removedEvent,pending_count=((IList)dependants.GetValue(first)).Count}));return 0;
        }
        if(args[0]=="recording_memory") {
            Console.WriteLine(json.Serialize(new {fits=VisualTools.RecordingFrameFits(192L*1024*1024,new Size(4096,4096)),
                rejects= !VisualTools.RecordingFrameFits(192L*1024*1024+1,new Size(4096,4096)),
                overflow_rejected= !VisualTools.RecordingFrameFits(Int64.MaxValue,new Size(4096,4096))}));return 0;
        }
        if(args[0].StartsWith("author_")) {
            using(var frame=new Bitmap(620,400)) {
                using(var g=Graphics.FromImage(frame)) {
                    g.Clear(Color.FromArgb(45,45,45));
                    for(int row=0;row<3;row++) {
                        int y=70+row*120; g.FillEllipse(Brushes.DimGray,472,y-18,36,36);
                        g.FillPolygon(Brushes.White,new Point[]{new Point(484,y-11),new Point(484,y+11),new Point(503,y)});
                        if(args[0]!="author_duplicate") using(var brush=new SolidBrush(row==0?Color.Cyan:row==1?Color.Red:Color.Lime)) {
                            g.FillRectangle(brush,40,y-14,130,28);
                            using(var font=new Font(FontFamily.GenericSansSerif,18,FontStyle.Bold)) g.DrawString("INSTANCE " + (row==0?"ALPHA":row==1?"BETA":"GAMMA"),font,Brushes.White,40,y-14);
                        }
                    }
                }
                var blocked=new List<Rectangle>();
                if(args[0]=="author_context_blocked") blocked.Add(new Rectangle(545,0,75,400));
                if(args[0]=="author_anchor_blocked") blocked.Add(new Rectangle(480,60,30,30));
                try {
                    var value=VisualTools.AuthorImage(frame,new Rectangle(468,46,48,48),new Point(492,70),blocked);
                    var selected=(Dictionary<string,object>)value["selection"];var anchor=(Dictionary<string,object>)value["anchor"];
                    Console.WriteLine(json.Serialize(new {accepted=true,selection=selected,diagnostic=value["capture_diagnostic"],
                        action_x=Convert.ToDouble(selected["x"])+Convert.ToDouble(anchor["x"])*Convert.ToDouble(selected["width"]),
                        action_y=Convert.ToDouble(selected["y"])+Convert.ToDouble(anchor["y"])*Convert.ToDouble(selected["height"])}));
                }catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new {accepted=false,code=error.Message}));}
                return 0;
            }
        }
        if(args[0]=="clip_context") {
            var original=new Rectangle(0,0,600,100);var anchor=new Point(480,50);
            var before=new Rectangle(0,0,100,100);var after=new Rectangle(550,0,50,100);
            var clipped=VisualTools.UncoveredContext(original,anchor,new List<Rectangle>{before,after});
            var covered=VisualTools.UncoveredContext(original,anchor,new List<Rectangle>{new Rectangle(470,40,20,20)});
            Console.WriteLine(json.Serialize(new {x=clipped.X,y=clipped.Y,width=clipped.Width,height=clipped.Height,
                anchor_preserved=clipped.Contains(anchor),old_frame_excluded=!clipped.IntersectsWith(before),current_frame_excluded=!clipped.IntersectsWith(after),covered_rejected=covered.IsEmpty}));return 0;
        }
        if(args[0].StartsWith("record_")) {
            using(var primary=new Bitmap(160,96)) using(var context=new Bitmap(384,128)) {
                using(var g=Graphics.FromImage(primary)) g.Clear(Color.Gray);
                using(var g=Graphics.FromImage(context)) { g.Clear(Color.Gray); g.FillRectangle(Brushes.White,15,12,90,12); g.FillRectangle(Brushes.Black,200,60,80,12); }
                if(args[0]=="record_horizontal") using(var g=Graphics.FromImage(primary)) for(int y=12;y<80;y+=16) g.FillRectangle(Brushes.White,0,y,160,4);
                if(args[0]=="record_vertical") using(var g=Graphics.FromImage(primary)) for(int x=12;x<144;x+=16) g.FillRectangle(Brushes.White,x,0,4,96);
                if(args[0]=="record_gradient") for(int y=0;y<96;y++) for(int x=0;x<160;x++) primary.SetPixel(x,y,Color.FromArgb(80+y,80+y,80+y));
                var result=VisualTools.RecordedImage(primary,new Point(80,48),args[0]=="record_context"?context:null,new Point(210,66),new Size(1724,1162),"screen");
                object value; var capture=result.TryGetValue("capture_diagnostic",out value)?value:null;
                Console.WriteLine(json.Serialize(new { accepted=result.ContainsKey("template_png"),rejected=result.ContainsKey("rejected_capture"),diagnostic=capture,
                    anchor=result.TryGetValue("anchor",out value)?value:null,source_size=result.TryGetValue("source_size",out value)?value:null })); return 0;
            }
        }
        if(args[0]=="region") {
            var small=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,true,false);
            var unnamed=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,false,false);
            var expanded=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),true,false,true);
            var graphical=VisualTools.RecordingRegion(new Size(1724,1162),new Point(1615,174),false,false,false);
            Console.WriteLine(json.Serialize(new { named_width=small.Width,unnamed_width=unnamed.Width,expanded_width=expanded.Width,graphical_width=graphical.Width,
                all_contain_anchor=small.Contains(1615,174)&&unnamed.Contains(1615,174)&&expanded.Contains(1615,174),
                all_inside_frame=small.Right<=1724&&unnamed.Right<=1724&&expanded.Right<=1724 }));return 0;
        }
        if(args[0].StartsWith("focus")) {
            var before = new Dictionary<string, object> { { "identity", "edit1" }, { "value", "old" } };
            var element = new Dictionary<string, object> { { "role", "Edit" }, { "is_password", false } };
            var after = new Dictionary<string, object> { { "identity", "edit1" }, { "value", "new" }, { "native_target", new Dictionary<string, object> { { "element", element } } } };
            if(args[0]=="focus_identity") after["identity"]="other";
            if(args[0]=="focus_native") after.Remove("native_target");
            if(args[0]=="focus_protected") after["protected"]=true;
            if(args[0]=="focus_password") element["is_password"]=true;
            if(args[0]=="focus_same") after["value"]="old";
            if(args[0]=="focus_empty") before["value"]="";
            if(args[0]=="focus_role") element["role"]="ComboBox";
            Console.WriteLine(json.Serialize(new { accepted=VisualTools.RecordedEditConfirmed(before,after,args[0]!="focus_append" && args[0]!="focus_empty") }));return 0;
        }
        using (var full = new Bitmap(160,120,PixelFormat.Format32bppRgb)) {
            using (var g = Graphics.FromImage(full)) g.Clear(args[0] == "sentinel" ? Color.Magenta : args[0] == "black" ? Color.Black : Color.White);
            if (args[0] == "valid" || args[0] == "outside") for(int y=0;y<120;y++) for(int x=0;x<160;x++) full.SetPixel(x,y,Color.FromArgb((x*13)%256,(y*17)%256,((x+y)*11)%256));
            if (args[0] == "sentinel") using (var g = Graphics.FromImage(full)) g.FillRectangle(Brushes.White,0,0,160,50);
            try {
                using(var crop=VisualTools.ValidateRenderedFrame(full,new Rectangle(-200,100,160,120),new Rectangle(args[0]=="outside" ? -220 : -192,106,144,108))) {
                    bool exact=true; for(int y=0;y<crop.Height;y++) for(int x=0;x<crop.Width;x++) if(crop.GetPixel(x,y).ToArgb()!=full.GetPixel(x+8,y+6).ToArgb()) exact=false;
                    Console.WriteLine(json.Serialize(new { width=crop.Width,height=crop.Height,exact=exact }));
                }
            } catch(InvalidOperationException e) { Console.WriteLine(json.Serialize(new { code=e.Message })); }
        }
        return 0;
    }
}
"""

@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class NativeRecordingCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-render-probe-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "render-probe.exe"
        probe = cls.root / "probe.cs"
        probe.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:RenderProbe", "/codepage:65001", *("/reference:"+r for r in refs), "/out:"+str(cls.helper), str(ROOT/"VisualTools.cs"), str(ROOT/"ProcessEditor.cs"), str(probe)],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise RuntimeError(result.stdout+result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self,mode,*args):
        result = subprocess.run([str(self.helper),mode,*args],capture_output=True,text=True,encoding="utf-8-sig",check=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)

    def test_recording_limits_and_editor_capacity_align_without_opening_windows(self):
        self.assertEqual(self.call("recording_limits"), {"event_min": True, "event_max": True, "event_zero": False,
            "event_over": False, "popup_last": True, "popup_over": False, "popup_active_over": False, "editor_steps": 1000})

    def test_large_repeated_images_and_pending_keys_preserve_rows_with_bounded_response(self):
        result = self.call("payload_budget", str(self.root / "large-recording-response.json"))
        self.assertTrue(result["limited"])
        self.assertEqual(result["count"], 300)
        self.assertGreater(result["manual"], 0)
        self.assertTrue(result["first_preserved"])
        self.assertEqual(result["last_reason"], "recording_payload_limit")
        self.assertEqual(result["last_requested"], "press_key")
        self.assertFalse(result["last_has_pixels"])
        self.assertFalse(result["last_has_key"])
        self.assertTrue(result["bounded"])
        self.assertTrue(result["escaping_counted"])
        self.assertLess(result["written_bytes"], 16 * 1024 * 1024)

    def test_native_editor_reads_thousand_row_payload_larger_than_old_startup_limit(self):
        from process_editor import ProcessDraft
        draft = ProcessDraft([{"program_id": "editor", "pid": 123, "window_id": 456, "label": "반복 업무 프로그램"}])
        draft.steps = [{"operation": "set_value", "program_id": "editor", "value": "긴 입력😀" * 1000} for _ in range(1000)]
        draft.labels = ["반복 대상😀" * 100] * 1000
        payload = {"draft": {"steps": draft.summaries()}, "programs": draft.program_views()}
        path = self.root / "large-editor-request.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertGreater(path.stat().st_size, 262144)
        self.assertLess(path.stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(self.call("editor_payload", str(path)), {"count": 1000, "last": 999})

    def test_negative_monitor_dwm_crop_preserves_every_source_pixel(self):
        self.assertEqual(self.call("valid"), {"width":144,"height":108,"exact":True})

    def test_white_and_black_unrendered_frames_are_not_templates(self):
        for mode in ("white","black"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"code":"target_render_incomplete"})

    def test_partially_rendered_sentinel_frame_is_rejected(self):
        self.assertEqual(self.call("sentinel"), {"code":"target_render_incomplete"})

    def test_geometry_outside_rendered_window_is_rejected(self):
        self.assertEqual(self.call("outside"), {"code":"target_capture_size_invalid"})

    def test_focused_edit_replacement_needs_same_owned_identity_and_new_nonprotected_value(self):
        for mode in ("focus", "focus_empty"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"accepted": True})
        for mode in ("focus_identity", "focus_native", "focus_protected", "focus_password", "focus_same", "focus_append", "focus_role"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"accepted": False})

    def test_horizontal_and_vertical_icon_detail_use_the_same_threshold(self):
        for mode in ("record_horizontal", "record_vertical"):
            with self.subTest(mode=mode):
                result = self.call(mode)
                self.assertTrue(result["accepted"])
                self.assertFalse(result["rejected"])
                self.assertEqual(result["diagnostic"]["candidate_count"], 1)
                self.assertEqual(result["anchor"], {"x": .5, "y": .5})

    def test_flat_capture_is_preserved_privately_but_never_becomes_a_target(self):
        result = self.call("record_flat")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["diagnostic"]["detail"], {"variance": 0, "horizontal_edge": 0, "vertical_edge": 0})
        self.assertIsNone(result["anchor"])

    def test_low_detail_primary_uses_own_captured_context_with_exact_anchor(self):
        result = self.call("record_context")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["diagnostic"]["candidate_count"], 2)
        self.assertTrue(result["diagnostic"]["context_expanded"])
        self.assertEqual(result["source_size"], {"width": 384, "height": 128})
        self.assertEqual(result["anchor"], {"x": 210/384, "y": 66/128})

    def test_weak_gradient_does_not_pass_new_vertical_edge_check(self):
        result = self.call("record_gradient")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["diagnostic"]["detail"]["vertical_edge"], 1)

    def test_unnamed_semantic_target_gets_context_without_moving_its_anchor(self):
        self.assertEqual(self.call("region"), {"named_width": 160, "unnamed_width": 384, "expanded_width": 640, "graphical_width": 384,
            "all_contain_anchor": True, "all_inside_frame": True})

    def test_small_repeated_play_icon_automatically_includes_row_context_without_anchor_drift(self):
        result = self.call("author_unique")
        self.assertTrue(result["accepted"], result)
        self.assertEqual((result["action_x"], result["action_y"]), (492, 70))
        self.assertGreater(result["selection"]["width"], 48)
        self.assertTrue(result["diagnostic"]["context_expanded"])
        self.assertTrue(result["diagnostic"]["source_frame_unique"])
        self.assertGreater(result["diagnostic"]["candidate_count"], 1)

    def test_indistinguishable_rows_are_not_accepted_by_nearest_coordinate(self):
        self.assertEqual(self.call("author_duplicate"), {"accepted": False, "code": "image_ambiguous"})

    def test_obstruction_far_from_action_can_shrink_context_without_discarding_recording(self):
        result = self.call("author_context_blocked")
        self.assertTrue(result["accepted"], result)
        self.assertEqual((result["action_x"], result["action_y"]), (492, 70))
        self.assertLessEqual(result["selection"]["x"] + result["selection"]["width"], 545)

    def test_actual_action_point_obstruction_remains_blocked(self):
        self.assertEqual(self.call("author_anchor_blocked"), {"accepted": False, "code": "image_occluded_requires_selection"})

    def test_old_frame_obstruction_and_current_obstruction_both_excluded(self):
        self.assertEqual(self.call("clip_context"), {"x": 100, "y": 0, "width": 450, "height": 100,
            "anchor_preserved": True, "old_frame_excluded": True, "current_frame_excluded": True, "covered_rejected": True})

    def test_full_frame_recording_has_a_hard_byte_budget_including_boundary_and_overflow(self):
        self.assertEqual(self.call("recording_memory"), {"fits": True, "rejects": True, "overflow_rejected": True})

    def test_delayed_image_completion_preserves_exact_key_owner_and_never_overwrites_action(self):
        result = self.call("async_complete")
        self.assertEqual(result["key"], {"operation": "press_key", "key": "TAB", "template_png": "first_pre_input_frame", "width": 48})
        self.assertEqual(result["other"], {"operation": "press_key", "key": "ENTER", "template_png": "second_frame"})
        self.assertEqual(result["removed"]["capture_issue"], "image_authoring_pending")
        self.assertEqual(result["pending_count"], 0)

    def test_double_click_merge_redirects_key_already_waiting_in_the_same_queue(self):
        self.assertEqual(self.call("async_redirect"), {"queued_key_redirected": True, "other_owner_preserved": True})

    def test_failed_image_resolution_marks_dependent_key_for_repair(self):
        result = self.call("async_failure")
        self.assertEqual(result["key"]["operation"], "manual_entry")
        self.assertEqual(result["key"]["reason"], "focus_target_requires_selection")
        self.assertEqual(result["key"]["capture_issue"], "image_ambiguous")
        self.assertEqual(result["key"]["key"], "TAB")

    def test_image_failure_does_not_erase_independently_identified_key_target(self):
        result = self.call("async_failure_native")
        self.assertEqual(result["key"]["operation"], "press_key")
        self.assertEqual(result["key"]["native_target"], {"identity": "exact_owned_edit"})

if __name__ == "__main__":
    unittest.main()
