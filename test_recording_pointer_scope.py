"""Native scope policies and own offscreen popup HWNDs; no injected input."""
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
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
internal static class PointerProbe {
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateWindowEx(uint ex,string cls,string title,uint style,int x,int y,int w,int h,IntPtr owner,IntPtr menu,IntPtr instance,IntPtr parameter);
 [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr hwnd);
 [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd,int command);
 static IntPtr Offscreen(IntPtr owner, bool child) {
  IntPtr hwnd=CreateWindowEx(0x08000080,"STATIC","",child?0x50000000u:0x90000000u,-32000,-32000,160,90,owner,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
  if(hwnd==IntPtr.Zero)throw new InvalidOperationException("fixture_window_failed:"+Marshal.GetLastWin32Error());return hwnd;
 }
 static object PopupEpisodes(string mode) {
  var handles=new List<IntPtr>();var targets=new List<VisualTools.Target>();
  BindingFlags flags=BindingFlags.Instance|BindingFlags.NonPublic;
  Type recorder=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
  object instance=FormatterServices.GetUninitializedObject(recorder);
  try {
   IntPtr main=Offscreen(IntPtr.Zero,false);handles.Add(main);
   var root=new VisualTools.Target(new Dictionary<string,object>{{"pid",System.Diagnostics.Process.GetCurrentProcess().Id},{"window_id",main.ToInt64()},{"program_id","fixture"},{"window_ref","main"},{"image_allowed",false}});targets.Add(root);
   recorder.GetField("targets",flags).SetValue(instance,targets);recorder.GetField("warnings",flags).SetValue(instance,new List<string>());
   recorder.GetField("popupPrefix",flags).SetValue(instance,"popup_fixture_");recorder.GetField("maximum",flags).SetValue(instance,100);
   recorder.GetField("events",flags).SetValue(instance,new List<Dictionary<string,object>>());
   FieldInfo pendingField=recorder.GetField("pending",flags);object pending=Activator.CreateInstance(pendingField.FieldType);pendingField.SetValue(instance,pending);
   IntPtr first=Offscreen(main,false), sibling=Offscreen(main,false), nested=Offscreen(first,false), unrelated=Offscreen(IntPtr.Zero,false);
   handles.Add(first);handles.Add(sibling);handles.Add(nested);handles.Add(unrelated);
   var discover=recorder.GetMethod("DiscoverPopup",flags,null,new[]{typeof(IntPtr)},null);
   foreach(IntPtr hwnd in new[]{first,sibling,nested,unrelated})discover.Invoke(instance,new object[]{hwnd});
   bool scoped=targets.Count==4 && targets[1].Hwnd==first && targets[2].Hwnd==sibling && targets[3].Hwnd==nested;
   string firstRef=targets[1].Window;discover.Invoke(instance,new object[]{first});discover.Invoke(instance,new object[]{nested});
   if(mode=="popup_siblings_nested")return new{exact_distinct=scoped,closest_owner=targets[3].Owner==targets[1],siblings_owner=targets[1].Owner==root&&targets[2].Owner==root,unrelated_excluded=targets.TrueForAll(t=>t.Hwnd!=unrelated),same_episode_deduplicated=targets.Count==4&&targets[1].Window==firstRef};
   if(mode=="popup_episode") {
    var original=targets[1];ShowWindow(first,0);ShowWindow(first,8);
    // Process the observed HIDE after the same HWND has become visible again.
    recorder.GetMethod("RetirePopupEpisode",flags).Invoke(instance,new object[]{first});
    discover.Invoke(instance,new object[]{first});var fresh=targets[targets.Count-1];
    return new{old_retired=original.Retired,new_reference=fresh.Hwnd==first&&fresh.Window!=original.Window&&!fresh.Retired,nested_retired=targets[3].Retired,main_valid=root.Valid()};
   }
   if(mode=="popup_child_hide") {
    IntPtr child=Offscreen(first,true);handles.Add(child);
    recorder.GetMethod("RetirePopupEpisode",flags).Invoke(instance,new object[]{child});
    recorder.GetMethod("RetirePopupEpisode",flags).Invoke(instance,new object[]{main});
    return new{menu_retained=!targets[1].Retired,main_retained=!root.Retired};
   }
   if(mode=="popup_fast_click") {
    IntPtr fast=Offscreen(first,false);handles.Add(fast);
    var method=recorder.GetMethod("UnboundPopupClickFor",flags);Type pointType=method.GetParameters()[1].ParameterType;object point=Activator.CreateInstance(pointType);
    bool kept=(bool)method.Invoke(instance,new object[]{fast,point,"click"});
    object item=pending.GetType().GetMethod("Peek").Invoke(pending,null);Type ptype=item.GetType();
    return new{kept=kept,manual=(string)ptype.GetField("Operation",flags).GetValue(item)=="manual_entry",needs_preimage=(string)ptype.GetField("Reason",flags).GetValue(item)=="image_capture_required",no_pixels=ptype.GetField("Source",flags).GetValue(item)==null,owner=ptype.GetField("Target",flags).GetValue(item)==targets[1]};
   }
   throw new InvalidOperationException(mode);
  } finally {foreach(var t in targets)t.Release();for(int i=handles.Count-1;i>=0;i--)DestroyWindow(handles[i]);}
 }
 static int Main(string[] args) {
  if(args[0].StartsWith("popup_")) { Console.WriteLine(new JavaScriptSerializer().Serialize(PopupEpisodes(args[0])));return 0; }
  if(args[0].StartsWith("relationship_")) {
   string relationMode=args[0];
   long destination=relationMode=="relationship_nested"?200:100;
   bool result=VisualTools.HasWindowRelationship(300,destination,42,
    delegate(long h){return relationMode=="relationship_foreign" && h==200?43:42;},
    delegate(long h){return h==300?200:relationMode=="relationship_cycle" && h==200?300:0;},
    delegate(long h){return h==200?(relationMode=="relationship_other_root"?999:relationMode=="relationship_cycle"?0:100):0;});
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {related=result})); return 0;
  }
  if(args[0].StartsWith("draft_candidate_")) {
   string draftMode=args[0]; bool same=true,current=true,foreground=true,ordinary=true,focus=true,secret=false; double age=1;
   if(draftMode=="draft_candidate_other_target")same=false;
   if(draftMode=="draft_candidate_reused_window")current=false;
   if(draftMode=="draft_candidate_other_foreground")foreground=false;
   if(draftMode=="draft_candidate_drag")ordinary=false;
   if(draftMode=="draft_candidate_focus_moved")focus=false;
   if(draftMode=="draft_candidate_protected")secret=true;
   if(draftMode=="draft_candidate_expired")age=30.001;
   if(draftMode=="draft_candidate_future")age=-1;
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {candidate=VisualTools.RecordingDraftInputCandidate(same,current,foreground,ordinary,focus,secret,age)}));return 0;
  }
  if(args[0].StartsWith("draft_fold_") || args[0]=="draft_protected_cleanup" || args[0].StartsWith("draft_reset_")) {
   var flags=BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance|BindingFlags.Static;
   var recorder=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
   var pendingType=recorder.GetNestedType("Pending",BindingFlags.NonPublic);
   var targetType=pendingType.GetField("Target",flags).FieldType;
   var target=FormatterServices.GetUninitializedObject(targetType);
   var pointer=Activator.CreateInstance(pendingType,true);var prep=Activator.CreateInstance(pendingType,true);var input=Activator.CreateInstance(pendingType,true);
   Action<object,string,object> set=(obj,key,value)=>pendingType.GetField(key,flags).SetValue(obj,value);
   foreach(var p in new object[]{prep,input}) {set(p,"Target",target);set(p,"ImageOwner",pointer);set(p,"DraftInputTarget",true);set(p,"AuthorCompleted",true);}
   set(prep,"Keys",new string[]{"CTRL","A"});set(prep,"Reason","focus_target_requires_selection");set(input,"Reason","unknown_input");set(input,"DraftPreparation",prep);
   var original=new Dictionary<string,object>{{"operation","click"}};
   var saved=new Dictionary<string,object>{{"operation","manual_entry"},{"reason","focus_target_requires_selection"}};set(prep,"Saved",saved);
   var recorded=new List<Dictionary<string,object>>{original,saved}; string foldMode=args[0];
   if(foldMode=="draft_fold_other_target")set(input,"Target",FormatterServices.GetUninitializedObject(targetType));
   if(foldMode=="draft_fold_other_click")set(input,"ImageOwner",Activator.CreateInstance(pendingType,true));
   if(foldMode=="draft_fold_protected")set(input,"Reason","protected_input");
   if(foldMode=="draft_fold_known_value")set(input,"Baseline",new Dictionary<string,object>{{"value","known"}});
   if(foldMode=="draft_fold_no_candidate")set(input,"DraftInputTarget",false);
   if(foldMode=="draft_fold_other_shortcut")set(prep,"Keys",new string[]{"CTRL","Z"});
   if(foldMode=="draft_fold_intervening_enter")recorded.Add(new Dictionary<string,object>{{"operation","manual_entry"},{"key","ENTER"}});
   if(foldMode=="draft_fold_rewritten_prep")saved["reason"]="recording_method_unverified";
   if(foldMode.StartsWith("draft_reset_")) {
    object instance=FormatterServices.GetUninitializedObject(recorder);
    recorder.GetField("lastPointer",flags).SetValue(instance,pointer);recorder.GetField("typing",flags).SetValue(instance,input);recorder.GetField("selectAllPreparation",flags).SetValue(instance,prep);
    if(foldMode=="draft_reset_native") {set(prep,"DraftInputTarget",false);set(input,"DraftInputTarget",false);}
    if(foldMode=="draft_reset_scroll")recorder.GetMethod("ResetPointerInput",flags).Invoke(instance,new object[]{true});
    else recorder.GetMethod("InvalidateDraftInput",flags).Invoke(instance,null);
    Console.WriteLine(new JavaScriptSerializer().Serialize(new {candidate_invalidated=!(bool)pendingType.GetField("DraftInputFocusValid",flags).GetValue(pointer),
     native_focus_preserved=(bool)pendingType.GetField("ImageFocusValid",flags).GetValue(pointer),
     typing_retained=recorder.GetField("typing",flags).GetValue(instance)!=null,preparation_retained=recorder.GetField("selectAllPreparation",flags).GetValue(instance)!=null}));
    GC.SuppressFinalize(instance);return 0;
   }
   if(foldMode=="draft_protected_cleanup") {
    var protectedRow=new Dictionary<string,object>{{"operation","manual_entry"},{"reason","protected_input"},{"input_target_candidate",true},{"template_png","prior pixels"}};set(input,"Saved",protectedRow);
    ((IList)pendingType.GetField("ImageDependants",flags).GetValue(pointer)).Add(input);
    recorder.GetMethod("ClearDraftInputCandidate",flags).Invoke(null,new object[]{input});
    recorder.GetMethod("CompleteImageDependants",flags).Invoke(null,new object[]{pointer,new Dictionary<string,object>{{"template_png","worker pixels"}},null,new List<Dictionary<string,object>>{protectedRow}});
    Console.WriteLine(new JavaScriptSerializer().Serialize(new {has_image=protectedRow.ContainsKey("template_png"),has_candidate=protectedRow.ContainsKey("input_target_candidate"),image_owner_cleared=pendingType.GetField("ImageOwner",flags).GetValue(input)==null}));return 0;
   }
   bool folded=(bool)recorder.GetMethod("CoalesceDraftPreparation",flags).Invoke(null,new object[]{input,recorded});
   recorded.Add(new Dictionary<string,object>{{"operation","manual_entry"},{"reason","unknown_input"}});
   if(foldMode=="draft_fold_then_enter")recorded.Add(new Dictionary<string,object>{{"operation","manual_entry"},{"key","ENTER"}});
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {folded=folded,events=recorded,preparation_retired=pendingType.GetField("Saved",flags).GetValue(prep)==null}));return 0;
  }
  if(args[0].StartsWith("retire_")) {
   var flags=BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance|BindingFlags.Static;
   var recorder=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
   var pendingType=recorder.GetNestedType("Pending",BindingFlags.NonPublic);
   var frameType=recorder.GetNestedType("SharedFrame",BindingFlags.NonPublic);
   var cachedType=recorder.GetNestedType("CachedFrame",BindingFlags.NonPublic);
   var pending=Activator.CreateInstance(pendingType,true);
   var pixels=new Bitmap(8,8); var older=new Bitmap(8,8);
   var source=Activator.CreateInstance(frameType,flags,null,new object[]{pixels},null);
   var historySource=Activator.CreateInstance(frameType,flags,null,new object[]{older},null);
   var cached=Activator.CreateInstance(cachedType,true);cachedType.GetField("Source",flags).SetValue(cached,historySource);
   var history=(IList)pendingType.GetField("BeforeHover",flags).GetValue(pending);history.Add(cached);
   bool running=args[0]=="retire_running";
   pendingType.GetField("Source",flags).SetValue(pending,running?null:source);
   pendingType.GetField("Saved",flags).SetValue(pending,new Dictionary<string,object>{{"operation","click"}});
   recorder.GetMethod("RetireFoldedPointer",flags).Invoke(null,new object[]{pending});
   bool removed=pendingType.GetField("Saved",flags).GetValue(pending)==null && (int)pendingType.GetField("Index",flags).GetValue(pending)==-1;
   bool retained=history.Count==1 && pixels.Width==8 && older.Width==8 && (long)frameType.GetField("RetainedBytes",flags).GetValue(null)==512;
   if(running)frameType.GetMethod("Release",flags).Invoke(source,null);
   recorder.GetMethod("DisposePending",flags).Invoke(null,new object[]{pending});
   pendingType.GetField("AuthorCompleted",flags).SetValue(pending,true);
   recorder.GetMethod("RetireFoldedPointer",flags).Invoke(null,new object[]{pending});
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {removed=removed,retained_until_worker=retained,released_bytes=(long)frameType.GetField("RetainedBytes",flags).GetValue(null),history_count=history.Count}));return 0;
  }
  if(args[0].StartsWith("observe_")) {
   string kind=args[0]; double age=10; bool pending=true;
   if(kind=="observe_expired_unrelated")pending=false;
   if(kind=="observe_recent") {age=2;pending=false;}
   if(kind=="observe_future")age=-1;
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=VisualTools.RecordingValueObservationAllowed(age,pending)}));return 0;
  }
  if(args[0].StartsWith("check_")) {
   string kind=args[0]; var click=DateTime.UtcNow.AddMilliseconds(-300); var observed=click.AddMilliseconds(100); var now=click.AddMilliseconds(300);
   bool exact=true,latest=true,foreground=true; int version=2,current=2; string role="CheckBox",property="selected"; object value=true;
   if(kind=="check_foreign_child") exact=false;
   if(kind=="check_historical") latest=false;
   if(kind=="check_other_foreground") foreground=false;
   if(kind=="check_later_input") current=3;
   if(kind=="check_before_sample") observed=click.AddMilliseconds(-1);
   if(kind=="check_future_sample") observed=now.AddMilliseconds(1);
   if(kind=="check_expired") now=click.AddMilliseconds(1201);
   if(kind=="check_other_role") role="Button";
   if(kind=="check_wrong_property") property="value";
   if(kind=="check_unknown_value") value=null;
   if(kind=="check_string_value") value="true";
   if(kind=="check_unchecked") value=false;
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=VisualTools.RecordingDesiredCheckWitness(exact,latest,foreground,version,current,click,observed,now,role,property,value)}));return 0;
  }
  if(args[0].StartsWith("combo_")) {
   string kind=args[0]; var popup=DateTime.UtcNow.AddMilliseconds(-300); var before=popup.AddMilliseconds(-100); var after=popup.AddMilliseconds(100);
   bool owner=true,list=true,tail=true,released=true,ordinary=true; int opened=2,clicked=3,current=3; object initial="W",final="N";
   if(kind=="combo_other_owner") owner=false;
   if(kind=="combo_other_list") list=false;
   if(kind=="combo_intervening_row") tail=false;
   if(kind=="combo_still_held") released=false;
   if(kind=="combo_opener_drag") ordinary=false;
   if(kind=="combo_intervening_key") clicked=4;
   if(kind=="combo_later_input") current=4;
   if(kind=="combo_no_before_value") initial=null;
   if(kind=="combo_late_baseline") before=popup;
   if(kind=="combo_before_selection") after=popup.AddMilliseconds(-1);
   if(kind=="combo_unchanged") final="W";
   if(kind=="combo_empty") final="";
   if(kind=="combo_slow_selection") before=popup.AddSeconds(-10);
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=VisualTools.RecordingNativeComboWitness(owner,list,tail,released,ordinary,opened,clicked,current,before,popup,after,initial,final)}));return 0;
  }
  if(args[0].StartsWith("semantic")) {
   string kind=args[0]; int[] pids={200,200,100}; long[] windows={0,52,42}; bool[] secret={false,false,false};
   int childPid=200; long childWindow=52; bool current=true;
   if(kind=="semantic_plain") {pids=new int[]{100,100};windows=new long[]{0,42};secret=new bool[]{false,false};childPid=0;childWindow=0;}
   if(kind=="semantic_foreign") pids[0]=300;
   if(kind=="semantic_same_pid_other_window") windows[1]=53;
   if(kind=="semantic_wrong_frame") windows[2]=43;
   if(kind=="semantic_password") secret[0]=true;
   if(kind=="semantic_protected_ancestor") secret[1]=true;
   if(kind=="semantic_replaced_child") current=false;
   if(kind=="semantic_unpinned_child") {childPid=0;childWindow=0;}
   if(kind=="semantic_missing_bridge") {pids=new int[]{200,100};windows=new long[]{0,42};secret=new bool[]{false,false};}
   if(kind=="semantic_host_into_child") {pids=new int[]{100,200,100};windows=new long[]{0,52,42};}
   Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=VisualTools.RecordingSemanticOwnership(pids,windows,secret,100,42,childPid,childWindow,current)}));return 0;
  }
  string mode=args[0]; long root=42, window=42; int hitPid=123, targetPid=123;
  bool valid=true, visible=true, minimized=false;
  Rectangle bounds=new Rectangle(-900,100,800,600); Point point=new Point(-850,120);
  if(mode=="same_process_other_root") root=43;
  if(mode=="foreign_process") hitPid=456;
  if(mode=="reused_hwnd") valid=false;
  if(mode=="retired_popup") valid=false;
  if(mode=="hidden") visible=false;
  if(mode=="minimized") minimized=true;
  if(mode=="outside_bounds") point=new Point(-99,120);
  if(mode=="empty_bounds") bounds=Rectangle.Empty;
  if(mode=="missing_root") root=0;
  if(mode=="missing_pid") hitPid=0;
  bool found=VisualTools.RecordingPointerMatches(root,hitPid,window,targetPid,valid,visible,minimized,bounds,point);
  Console.WriteLine(new JavaScriptSerializer().Serialize(new {accepted=found}));return 0;
 }
}
"""


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows .NET compiler required")
class RecordingPointerScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cua-pointer-scope-")
        cls.root = Path(cls.directory.name)
        cls.helper = cls.root / "pointer.exe"
        source = cls.root / "probe.cs"
        source.write_text(PROBE, encoding="utf-8")
        refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Web.Extensions.dll"]
        refs += [str(CSC.parent / "WPF" / name) for name in ("WindowsBase.dll", "UIAutomationClient.dll", "UIAutomationTypes.dll")]
        result = subprocess.run([str(CSC), "/nologo", "/target:exe", "/main:PointerProbe", "/codepage:65001",
            *("/reference:" + ref for ref in refs), "/out:" + str(cls.helper), str(ROOT / "VisualTools.cs"), str(source)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, mode):
        result = subprocess.run([str(self.helper), mode], capture_output=True, text=True, check=True,
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)

    def test_exact_registered_root_is_accepted_before_foreground_activation(self):
        self.assertEqual(self.call("inactive_registered"), {"accepted": True})

    def test_owner_parent_relationship_retains_nested_business_window(self):
        for mode in ("relationship_parent", "relationship_nested"):
            self.assertEqual(self.call(mode), {"related":True})
        for mode in ("relationship_foreign", "relationship_other_root", "relationship_cycle"):
            self.assertEqual(self.call(mode), {"related":False})

    def test_unknown_input_candidate_requires_recent_exact_target_and_no_scope_or_focus_change(self):
        self.assertEqual(self.call("draft_candidate_exact"), {"candidate": True})
        for suffix in ("other_target", "reused_window", "other_foreground", "drag", "focus_moved", "protected", "expired", "future"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call("draft_candidate_" + suffix), {"candidate": False})

    def test_unknown_ctrl_a_and_typing_fold_but_actual_enter_remains_a_separate_step(self):
        result = self.call("draft_fold_then_enter")
        self.assertTrue(result["folded"])
        self.assertTrue(result["preparation_retired"])
        self.assertEqual(result["events"], [{"operation": "click"},
            {"operation": "manual_entry", "reason": "unknown_input"}, {"operation": "manual_entry", "key": "ENTER"}])
        self.assertNotIn("value", json.dumps(result))
        self.assertNotIn("native_target", json.dumps(result))

    def test_unknown_input_does_not_fold_unrelated_protected_or_intervening_actions(self):
        for suffix in ("other_target", "other_click", "protected", "known_value", "no_candidate", "other_shortcut", "intervening_enter", "rewritten_prep"):
            with self.subTest(suffix=suffix):
                result = self.call("draft_fold_" + suffix)
                self.assertFalse(result["folded"])
                self.assertFalse(result["preparation_retired"])
                self.assertGreaterEqual(len(result["events"]), 3)

    def test_later_password_detection_removes_candidate_and_pending_worker_cannot_restore_image(self):
        self.assertEqual(self.call("draft_protected_cleanup"),
                         {"has_image": False, "has_candidate": False, "image_owner_cleared": True})

    def test_scope_or_navigation_reset_drops_only_untrusted_candidate_continuation(self):
        self.assertEqual(self.call("draft_reset_unknown"), {"candidate_invalidated": True,
                         "native_focus_preserved": True, "typing_retained": False, "preparation_retained": False})
        self.assertEqual(self.call("draft_reset_native"), {"candidate_invalidated": True,
                         "native_focus_preserved": True, "typing_retained": True, "preparation_retained": True})

    def test_scroll_invalidates_pre_scroll_click_candidate_without_promoting_a_new_target(self):
        self.assertEqual(self.call("draft_reset_scroll"), {"candidate_invalidated": True,
                         "native_focus_preserved": True, "typing_retained": False, "preparation_retained": False})

    def test_already_owned_popup_uses_its_exact_root_and_supports_negative_monitor_coordinates(self):
        self.assertEqual(self.call("owned_popup"), {"accepted": True})

    def test_same_process_unregistered_root_and_foreign_process_are_rejected(self):
        for mode in ("same_process_other_root", "foreign_process", "missing_root", "missing_pid"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": False})

    def test_lifetime_visibility_geometry_and_retired_popup_are_rechecked(self):
        for mode in ("reused_hwnd", "retired_popup", "hidden", "minimized", "outside_bounds", "empty_bounds"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": False})

    def test_hosted_semantic_chain_crosses_only_pinned_core_window_into_its_frame(self):
        for mode in ("semantic_pinned", "semantic_plain"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": True})

    def test_hosted_semantics_reject_foreign_sibling_missing_bridge_replacement_and_password(self):
        for suffix in ("foreign", "same_pid_other_window", "wrong_frame", "password", "protected_ancestor",
                       "replaced_child", "unpinned_child", "missing_bridge", "host_into_child"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call("semantic_" + suffix), {"accepted": False})

    def test_exact_latest_checkbox_click_records_final_boolean_without_requiring_old_value(self):
        for mode in ("check_checked", "check_unchecked"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": True})

    def test_checkbox_never_promotes_historical_foreign_stale_or_non_boolean_sample(self):
        for suffix in ("foreign_child", "historical", "other_foreground", "later_input", "before_sample", "future_sample",
                       "expired", "other_role", "wrong_property", "unknown_value", "string_value"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call("check_" + suffix), {"accepted": False})

    def test_offscreen_same_class_siblings_and_nested_use_exact_hwnds(self):
        self.assertEqual(self.call("popup_siblings_nested"), {"exact_distinct": True, "closest_owner": True,
                         "siblings_owner": True, "unrelated_excluded": True, "same_episode_deduplicated": True})

    def test_offscreen_hide_show_same_hwnd_starts_new_visibility_episode(self):
        self.assertEqual(self.call("popup_episode"), {"old_retired": True, "new_reference": True,
                         "nested_retired": True, "main_valid": True})

    def test_offscreen_child_hide_cannot_retire_menu_or_main(self):
        self.assertEqual(self.call("popup_child_hide"), {"menu_retained": True, "main_retained": True})

    def test_offscreen_fast_click_without_preimage_is_preserved_as_repair(self):
        self.assertEqual(self.call("popup_fast_click"), {"kept": True, "manual": True,
                         "needs_preimage": True, "no_pixels": True, "owner": True})

    def test_native_combo_pair_can_fold_once_when_final_value_changed(self):
        self.assertEqual(self.call("combo_exact_pair"), {"accepted": True})

    def test_folded_popup_keeps_queued_and_running_image_pixels_until_author_cleanup(self):
        for mode in ("retire_queued", "retire_running"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"removed": True, "retained_until_worker": True,
                                                  "released_bytes": 0, "history_count": 0})

    def test_slow_selection_keeps_exact_pending_pair_but_not_stale_unrelated_samples(self):
        for mode in ("observe_exact_slow_pair", "observe_recent", "combo_slow_selection"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": True})
        for mode in ("observe_expired_unrelated", "observe_future"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": False})

    def test_combo_preserves_unknown_gestures_and_unproven_popup_relationship(self):
        for suffix in ("other_owner", "other_list", "intervening_row", "still_held", "opener_drag", "intervening_key", "later_input",
                       "no_before_value", "late_baseline", "before_selection", "unchanged", "empty"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call("combo_" + suffix), {"accepted": False})

    def test_image_capable_native_combo_proof_imports_one_selection_without_popup_replay(self):
        from test_process_editor import RecordingSemanticTests
        self.assertEqual(self.call("combo_exact_pair"), {"accepted": True})
        fixture = RecordingSemanticTests(); fixture.setUp()
        event, snapshots = fixture.event("select_option", "N")
        self.assertIn("template_png", event)
        event.update(requested_operation="click", selection_method="native_combobox_list_click", recorded_pointer_actions=2,
            pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": True},
            event_evidence={"source": "uia_recording", "identity": "exact-native-control", "after_input": True})
        fixture.draft.recorded([event], snapshots=snapshots)
        self.assertEqual([step["operation"] for step in fixture.draft.steps], ["select_option"])
        self.assertEqual(fixture.draft.steps[0]["value"], "N")
        self.assertNotIn("image_target", fixture.draft.steps[0])
        self.assertFalse(fixture.draft.recording_warnings)
        fixture.draft.validate()

    def test_observed_selection_alone_cannot_upgrade_old_unresolved_receipt(self):
        from test_process_editor import RecordingSemanticTests
        fixture = RecordingSemanticTests(); fixture.setUp()
        event, snapshots = fixture.event("select_option", "N")
        for key in ("native_target", "after", "value"):
            event.pop(key, None)
        event.update(operation="click", observed_selection="N", requested_operation="click",
            pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": True})
        fixture.draft.recorded([event], snapshots=snapshots)
        self.assertEqual([step["operation"] for step in fixture.draft.steps], ["image_click", "checkpoint"])
        self.assertNotIn("selector", fixture.draft.steps[0])

    def test_uia_only_checkbox_and_combo_import_without_claiming_pixels_or_duplicate_clicks(self):
        from test_process_editor import RecordingSemanticTests
        for operation, value in (("set_checked", True), ("select_option", "N")):
            with self.subTest(operation=operation):
                fixture = RecordingSemanticTests(); fixture.setUp()
                event, snapshots = fixture.event(operation, value)
                event.pop("template_png")
                event.update(requested_operation="click", capture_issue="image_replay_unavailable",
                    pointer_evidence={"source": "native_mouse_hook", "operation": "click", "before_input": False},
                    event_evidence={"source": "uia_recording", "identity": "exact-native-control", "after_input": True})
                if operation == "set_checked":
                    event.update(native_child_before_input=True, value_observation="desired_state_after_exact_click")
                else:
                    event.update(selection_method="native_combobox_list_click", recorded_pointer_actions=2)
                fixture.draft.recorded([event], snapshots=snapshots)
                self.assertEqual([step["operation"] for step in fixture.draft.steps], [operation])
                self.assertFalse(fixture.draft.recording_warnings)
                self.assertEqual(fixture.draft.steps[0]["expect"][0]["equals"], value)
                self.assertFalse(event["pointer_evidence"]["before_input"])
                fixture.draft.validate()


if __name__ == "__main__":
    unittest.main()
