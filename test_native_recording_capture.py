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
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
internal static class RenderProbe {
    static int Main(string[] args) {
        var json = new JavaScriptSerializer();
        if(args[0]=="recorder_waiting_cache") {
            BindingFlags flags=BindingFlags.Instance|BindingFlags.NonPublic;
            Type recorderType=typeof(VisualTools).GetNestedType("Recorder",BindingFlags.NonPublic);
            Type cacheType=recorderType.GetNestedType("CachedFrame",BindingFlags.NonPublic),sharedType=recorderType.GetNestedType("SharedFrame",BindingFlags.NonPublic);
            object recorder=System.Runtime.Serialization.FormatterServices.GetUninitializedObject(recorderType);
            object target=System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(VisualTools.Target));
            DateTime now=DateTime.UtcNow,request=now.AddMilliseconds(-400);
            var pixels=new Bitmap(24,12);pixels.SetPixel(4,5,Color.Red);
            pixels.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",now.AddMinutes(-1),request.AddMilliseconds(20),0,request,7);
            object shared=Activator.CreateInstance(sharedType,flags,null,new object[]{pixels},null),cached=Activator.CreateInstance(cacheType,true);
            cacheType.GetField("Source",flags).SetValue(cached,shared);cacheType.GetField("At",flags).SetValue(cached,request);
            cacheType.GetField("InputAt",flags).SetValue(cached,request);cacheType.GetField("InputVersion",flags).SetValue(cached,7);
            var cacheField=recorderType.GetField("frameCache",flags);var cache=(IDictionary)Activator.CreateInstance(cacheField.FieldType);cache.Add(target,cached);cacheField.SetValue(recorder,cache);
            recorderType.GetField("inputVersion",flags).SetValue(recorder,7);recorderType.GetField("inputChangedAt",flags).SetValue(recorder,request.AddTicks(-1));
            // ReleaseFrame leaves the remembered target in place. The original
            // hook skipped this still-usable cache when the active frame was null.
            recorderType.GetField("frameTarget",flags).SetValue(recorder,target);
            var restore=recorderType.GetMethod("RestoreClickFrame",flags);var unavailable=recorderType.GetMethod("CaptureUnavailable",flags);
            var active=recorderType.GetField("frame",flags);var release=recorderType.GetMethod("ReleaseFrame",flags);
            restore.Invoke(recorder,new object[]{target});bool restored=active.GetValue(recorder)==pixels;
            unavailable.Invoke(recorder,new object[]{target,new InvalidOperationException("target_render_waiting")});bool waitingKept=active.GetValue(recorder)==pixels;
            var diagnostic=new Dictionary<string,object>();recorderType.GetMethod("AppendFrameEpochDiagnostic",flags).Invoke(recorder,new object[]{target,diagnostic,now});
            recorderType.GetField("inputVersion",flags).SetValue(recorder,8);
            unavailable.Invoke(recorder,new object[]{target,new InvalidOperationException("target_render_waiting")});bool changedCleared=active.GetValue(recorder)==null;
            restore.Invoke(recorder,new object[]{target});bool changedNotRestored=active.GetValue(recorder)==null;
            recorderType.GetField("inputVersion",flags).SetValue(recorder,7);
            bool allScopeCleared=true;
            foreach(string error in new[]{"target_requires_foreground","target_changed","target_unavailable","recording_capture_backlog"}) {
                restore.Invoke(recorder,new object[]{target});unavailable.Invoke(recorder,new object[]{target,new InvalidOperationException(error)});
                allScopeCleared &= active.GetValue(recorder)==null;
            }
            DateTime old=now.AddSeconds(-2);pixels.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",old,old,0,old,7);
            cacheType.GetField("At",flags).SetValue(cached,old);cacheType.GetField("InputAt",flags).SetValue(cached,old);
            recorderType.GetField("inputChangedAt",flags).SetValue(recorder,old.AddTicks(-1));
            restore.Invoke(recorder,new object[]{target});bool staleNotRestored=active.GetValue(recorder)==null;
            release.Invoke(recorder,new object[0]);sharedType.GetMethod("Release",flags).Invoke(shared,new object[0]);
            Console.WriteLine(json.Serialize(new{restored_same_target_null_frame=restored,pending_kept_original_pixels=waitingKept,
                changed_epoch_cleared=changedCleared,changed_epoch_not_restored=changedNotRestored,scope_failures_cleared=allScopeCleared,
                stale_not_restored=staleNotRestored,diagnostic=diagnostic,
                retained=sharedType.GetField("RetainedBytes",BindingFlags.Static|BindingFlags.NonPublic).GetValue(null)}));return 0;
        }
        if(args[0]=="recording_request_retention") {
            DateTime now=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc),request=now.AddMilliseconds(-500),received=now.AddMilliseconds(-480),source=now.AddMinutes(-1);
            var frame=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,request,7);
            var boundary=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,now.AddMilliseconds(-1000),7);
            var expired=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,now.AddMilliseconds(-1001),7);
            var oneShot=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,request,-1);
            var future=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,now.AddTicks(1),7);
            var legacy=new VisualTools.CapturedFrameInfo("print_window",request);
            Console.WriteLine(json.Serialize(new{
                recording=VisualTools.RecordingObservationUsable(frame,now,request.AddTicks(-1),7),
                generic=VisualTools.CapturedObservationUsable(frame,now,request.AddTicks(-1),7),
                exact_boundary=VisualTools.RecordingObservationUsable(boundary,now,DateTime.MinValue,7),
                expired=VisualTools.RecordingObservationUsable(expired,now,DateTime.MinValue,7),
                input_after_request=VisualTools.RecordingObservationUsable(frame,now,request.AddTicks(1),7),
                changed_epoch=VisualTools.RecordingObservationUsable(frame,now,request,8),
                one_shot=VisualTools.RecordingObservationUsable(oneShot,now,DateTime.MinValue,7),
                legacy=VisualTools.RecordingObservationUsable(legacy,now,DateTime.MinValue,7),
                future=VisualTools.RecordingObservationUsable(future,now,DateTime.MinValue,7),
                hook=VisualTools.RecordingFrameIssue(true,true,request,now,request.AddTicks(-1),true,true,frame),
                source_preserved=frame.At==source,request_preserved=frame.RequestAt==request,received_preserved=frame.ReceivedAt==received}));return 0;
        }
        if(args[0]=="capture_request_epoch") {
            DateTime now=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc),request=now.AddMilliseconds(-40),received=now.AddMilliseconds(-20),source=now.AddSeconds(-60);
            var frame=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,request,7);
            var oldRequest=new VisualTools.CapturedFrameInfo("windows_graphics_capture",source,received,0,now.AddMilliseconds(-251),7);
            bool pending=true;bool noFrame=VisualTools.FirstCaptureFrame(ref pending,false),first=VisualTools.FirstCaptureFrame(ref pending,true),second=VisualTools.FirstCaptureFrame(ref pending,true);
            bool newSession=true,newFirst=VisualTools.FirstCaptureFrame(ref newSession,true);double lead;
            DateTime historical=VisualTools.AwaitCaptureTime(10000000,10000000,delegate{return 610000000;},delegate{return now;},delegate(int ms){throw new Exception("unexpected_wait");},true,out lead);
            Console.WriteLine(json.Serialize(new {
                static_source=VisualTools.CapturedObservationUsable(frame,now,request.AddTicks(-1),7),source_preserved=frame.At==source,
                request_preserved=frame.ObservedAt==request,received_preserved=frame.ReceivedAt==received,
                intervening_input=VisualTools.CapturedObservationUsable(frame,now,request.AddMilliseconds(1),7),
                same_tick_new_epoch=VisualTools.CapturedObservationUsable(frame,now,request,8),
                expired_request=VisualTools.CapturedObservationUsable(oldRequest,now,now.AddSeconds(-1),7),
                no_frame=noFrame,first=first,second=second,new_session_first=newFirst,old_tag_still_request=frame.ObservedAt==request,
                historical_render_age_ms=now.Subtract(historical).TotalMilliseconds}));return 0;
        }
        if(args[0]=="future_capture_wait") {
            DateTime initial=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc);long counter=10000000;int delays=0;double lead;
            DateTime at=VisualTools.AwaitCaptureTime(10070000,10000000,delegate{return counter;},delegate{return initial.AddTicks(counter-10000000);},delegate(int ms){counter+=ms*10000;delays++;},out lead);
            var info=new VisualTools.CapturedFrameInfo("windows_graphics_capture",at,initial,lead);
            bool unreasonable=false,frozen=false;int frozenDelays=0;
            try{double x;VisualTools.AwaitCaptureTime(10500001,10000000,delegate{return 10000000;},delegate{return initial;},delegate(int ms){throw new Exception("must_not_wait");},out x);}catch(VisualTools.CaptureChangedException e){unreasonable=e.Reason=="capture_time_future";}
            try{double x;VisualTools.AwaitCaptureTime(10000001,10000000,delegate{return 10000000;},delegate{return initial;},delegate(int ms){frozenDelays++;},out x);}catch(VisualTools.CaptureChangedException e){frozen=e.Reason=="capture_time_future";}
            Console.WriteLine(json.Serialize(new{delays=delays,lead_ms=lead,source_ms=at.Subtract(initial).TotalMilliseconds,
                input_before_acquisition=VisualTools.CapturedFrameAfterInput(info.InputAt,initial.AddTicks(-1)),
                input_during_dequeue_or_wait=VisualTools.CapturedFrameAfterInput(info.InputAt,initial.AddMilliseconds(3)),
                source_unchanged=info.At==at,unreasonable_rejected=unreasonable,frozen_rejected=frozen,frozen_delays=frozenDelays}));return 0;
        }
        if(args[0]=="capture_time_reasons") {
            var results=new List<string>();DateTime now=DateTime.UtcNow;
            foreach(long ticks in new long[]{-1,10000001,0})try{VisualTools.CaptureTimeFromQpc(ticks,ticks==0?7500001:2500000,2500000,now);}
            catch(VisualTools.CaptureChangedException error){results.Add(error.Message+":"+error.Reason);}
            Console.WriteLine(json.Serialize(results));return 0;
        }
        if(args[0]=="capture_refresh") {
            DateTime now=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc),old=now.AddSeconds(-2);
            Console.WriteLine(json.Serialize(new{
                cold=VisualTools.RecordingCaptureRefreshDue(DateTime.MinValue,old,now,false),
                fresh=VisualTools.RecordingCaptureRefreshDue(now.AddMilliseconds(-100),old,now,false),
                static_scene=VisualTools.RecordingCaptureRefreshDue(old,old,now,false),
                request_recent=VisualTools.RecordingCaptureRefreshDue(old,now.AddMilliseconds(-100),now,false),
                awaiting_early=VisualTools.RecordingCaptureRefreshDue(old,now.AddMilliseconds(-499),now,true),
                awaiting_retry=VisualTools.RecordingCaptureRefreshDue(old,now.AddMilliseconds(-500),now,true),
                future=VisualTools.RecordingCaptureRefreshDue(now.AddTicks(1),old,now,false),
                old_pixels_still_stale=VisualTools.CapturedFrameFresh(old,now),
                old_pixels_before_input=VisualTools.CapturedFrameAfterInput(old,now.AddMilliseconds(-50))}));return 0;
        }
        if(args[0]=="capture_slot") {
            var slot=new VisualTools.LatestCapture();
            var first=new Bitmap(24,12);first.SetPixel(4,5,Color.Red);slot.Publish(first);
            var lease=slot.Lease();
            var second=new Bitmap(24,12);second.SetPixel(4,5,Color.Blue);slot.Publish(second);
            bool oldStable=lease.Image.GetPixel(4,5).ToArgb()==Color.Red.ToArgb();
            var newest=slot.Lease();bool latestBlue=newest.Image.GetPixel(4,5).ToArgb()==Color.Blue.ToArgb();
            long two=VisualTools.CapturePixels.RetainedBytes;slot.Dispose();
            bool stoppedEmpty=slot.Lease()==null;
            bool heldAfterStop=newest.Image.GetPixel(4,5).ToArgb()==Color.Blue.ToArgb();
            bool lateRejected=!slot.Publish(new Bitmap(24,12));
            lease.Release();newest.Release();slot.Dispose();
            Console.WriteLine(json.Serialize(new{old_stable=oldStable,latest=latestBlue,two_frames=two==24*12*4*2,
                stopped_empty=stoppedEmpty,held_after_stop=heldAfterStop,late_rejected=lateRejected,retained=VisualTools.CapturePixels.RetainedBytes}));return 0;
        }
        if(args[0]=="capture_slot_nonblocking") {
            var slot=new VisualTools.LatestCapture();slot.Publish(new Bitmap(16,16));
            var entered=new System.Threading.ManualResetEvent(false);var release=new System.Threading.ManualResetEvent(false);
            var worker=new System.Threading.Thread(delegate(){entered.Set();release.WaitOne();slot.Publish(new Bitmap(16,16));});worker.Start();entered.WaitOne();
            // The producer is deliberately stalled outside publication. A UI
            // lease must still return without waiting for GPU/producer work.
            var watch=System.Diagnostics.Stopwatch.StartNew();var lease=slot.Lease();watch.Stop();
            bool available=lease!=null;lease.Release();slot.Dispose();release.Set();worker.Join();
            Console.WriteLine(json.Serialize(new{available=available,elapsed_ms=watch.ElapsedMilliseconds,retained=VisualTools.CapturePixels.RetainedBytes}));
            entered.Dispose();release.Dispose();return 0;
        }
        if(args[0]=="capture_pump_identity") {
            Type type=typeof(VisualTools).GetNestedType("CapturePump",BindingFlags.NonPublic);
            var pump=System.Runtime.Serialization.FormatterServices.GetUninitializedObject(type);
            var owner=(VisualTools.Target)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(VisualTools.Target));
            var other=(VisualTools.Target)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(VisualTools.Target));
            Rectangle raw=new Rectangle(0,0,24,12),visible=raw;var slot=new VisualTools.LatestCapture();
            type.GetField("Owner",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(pump,owner);
            type.GetField("Raw",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(pump,raw);
            type.GetField("Visible",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(pump,visible);
            type.GetField("latest",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(pump,slot);
            var matches=type.GetMethod("Matches",BindingFlags.Instance|BindingFlags.NonPublic);
            bool exact=(bool)matches.Invoke(pump,new object[]{owner,raw,visible});
            bool otherOwner=(bool)matches.Invoke(pump,new object[]{other,raw,visible});
            bool moved=(bool)matches.Invoke(pump,new object[]{owner,new Rectangle(1,0,24,12),visible});
            var snap=type.GetMethod("Snapshot",BindingFlags.Instance|BindingFlags.NonPublic);
            DateTime now=DateTime.UtcNow;var pixels=new Bitmap(24,12);pixels.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",now.AddMilliseconds(-30));slot.Publish(pixels);
            bool preInputRejected=false;try{snap.Invoke(pump,new object[]{owner,raw,visible,now});}catch(TargetInvocationException e){preInputRejected=e.InnerException.Message=="target_render_waiting";}
            var waited=new Bitmap(24,12);waited.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",DateTime.UtcNow,now.AddMilliseconds(-30),7);slot.Publish(waited);
            bool acquisitionRejected=false;try{snap.Invoke(pump,new object[]{owner,raw,visible,now});}catch(TargetInvocationException e){acquisitionRejected=e.InnerException.Message=="target_render_waiting";}
            var old=new Bitmap(24,12);old.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",now.AddSeconds(-1));slot.Publish(old);
            bool staleRejected=false;try{snap.Invoke(pump,new object[]{owner,raw,visible,DateTime.MinValue});}catch(TargetInvocationException e){staleRejected=e.InnerException.Message=="target_render_waiting";}
            type.GetField("stopping",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(pump,true);
            bool stopped=(bool)matches.Invoke(pump,new object[]{owner,raw,visible});slot.Dispose();
            Console.WriteLine(json.Serialize(new{exact=exact,other_owner=otherOwner,moved=moved,pre_input_rejected=preInputRejected,acquisition_rejected=acquisitionRejected,stale_rejected=staleRejected,stopped=stopped,retained=VisualTools.CapturePixels.RetainedBytes}));return 0;
        }
        if(args[0]=="frame_failure") {
            DateTime now=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc),recent=now.AddMilliseconds(-40),prior=now.AddSeconds(-1);
            Console.WriteLine(json.Serialize(new {missing=VisualTools.RecordingFrameIssue(false,true,recent,now,prior,true,true),
                target=VisualTools.RecordingFrameIssue(true,false,recent,now,prior,true,true),
                stale=VisualTools.RecordingFrameIssue(true,true,prior,now,prior,true,true),
                input=VisualTools.RecordingFrameIssue(true,true,recent,now,now,true,true),
                geometry=VisualTools.RecordingFrameIssue(true,true,recent,now,prior,false,true),
                outside=VisualTools.RecordingFrameIssue(true,true,recent,now,prior,true,false),
                usable=VisualTools.RecordingFrameIssue(true,true,recent,now,prior,true,true)}));return 0;
        }
        if(args[0]=="capture_freshness") {
            DateTime now=new DateTime(2026,10,17,12,0,0,DateTimeKind.Utc);
            DateTime actual=VisualTools.CaptureTimeFromQpc(9900000,2500000,2500000,now);
            Type jobType=typeof(VisualTools).GetNestedType("RenderJob",BindingFlags.NonPublic);object job=Activator.CreateInstance(jobType,true);
            var fresh=typeof(VisualTools).GetMethod("RenderJobFresh",BindingFlags.Static|BindingFlags.NonPublic);
            using(var image=new Bitmap(16,16)) {
                jobType.GetField("Image",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,image);
                jobType.GetField("Started",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,now.AddSeconds(-30));
                image.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",actual);
                bool slowStartup=(bool)fresh.Invoke(null,new object[]{job,now});
                jobType.GetField("Started",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,now);
                image.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",now.AddMilliseconds(-251));
                bool recentCompletionOldPixels=(bool)fresh.Invoke(null,new object[]{job,now});
                image.Tag=new VisualTools.CapturedFrameInfo("windows_graphics_capture",now.AddTicks(1));
                bool future=(bool)fresh.Invoke(null,new object[]{job,now});
                bool invalidFuture=false,invalidOld=false;
                try{VisualTools.CaptureTimeFromQpc(10000001,2500000,2500000,now);}catch(InvalidOperationException){invalidFuture=true;}
                try{VisualTools.CaptureTimeFromQpc(0,7500001,2500000,now);}catch(InvalidOperationException){invalidOld=true;}
                Console.WriteLine(json.Serialize(new {qpc_age_ms=(now-actual).TotalMilliseconds,slow_startup_fresh=slowStartup,
                    recent_completion_old_pixels=recentCompletionOldPixels,future_fresh=future,future_qpc_rejected=invalidFuture,old_qpc_rejected=invalidOld,
                    prior_input=VisualTools.CapturedFrameAfterInput(actual,now.AddMilliseconds(-20)),
                    newer_input=VisualTools.CapturedFrameAfterInput(actual,now.AddMilliseconds(-5))}));
            }
            ((System.Threading.ManualResetEvent)jobType.GetField("Completed",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(job)).Dispose();return 0;
        }
        if(args[0]=="backend_diagnostic") {
            var owner=(VisualTools.Target)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(VisualTools.Target));
            var other=(VisualTools.Target)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(VisualTools.Target));
            Type jobType=typeof(VisualTools).GetNestedType("RenderJob",BindingFlags.NonPublic);object job=Activator.CreateInstance(jobType,true);
            jobType.GetField("Owner",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,owner);
            jobType.GetField("GraphicsStage",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,"map_texture");
            jobType.GetField("GraphicsHResult",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(job,unchecked((int)0x887A000A));
            var jobs=(IDictionary)typeof(VisualTools).GetField("renderJobs",BindingFlags.Static|BindingFlags.NonPublic).GetValue(null);jobs.Add(IntPtr.Zero,job);
            var append=typeof(VisualTools).GetMethod("AppendCaptureBackendDiagnostic",BindingFlags.Static|BindingFlags.NonPublic);
            var first=new Dictionary<string,object>();var differentOwner=new Dictionary<string,object>();var completed=new Dictionary<string,object>();
            append.Invoke(null,new object[]{owner,first});append.Invoke(null,new object[]{other,differentOwner});
            var signal=(System.Threading.ManualResetEvent)jobType.GetField("Completed",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(job);signal.Set();
            append.Invoke(null,new object[]{owner,completed});jobs.Clear();signal.Dispose();
            Console.WriteLine(json.Serialize(new{pending=first,other=differentOwner,completed=completed}));return 0;
        }
        if(args[0]=="full_coverage") {
            var bounds=new Rectangle(-200,30,600,400);
            Console.WriteLine(json.Serialize(new {
                one=VisualTools.RecordingFrameFullyCovered(bounds,new List<Rectangle>{new Rectangle(-300,0,800,500)}),
                joined=VisualTools.RecordingFrameFullyCovered(bounds,new List<Rectangle>{new Rectangle(-200,30,300,400),new Rectangle(100,30,300,400)}),
                gap=VisualTools.RecordingFrameFullyCovered(bounds,new List<Rectangle>{new Rectangle(-200,30,299,400),new Rectangle(100,30,300,400)}),
                toolbar=VisualTools.RecordingFrameFullyCovered(bounds,new List<Rectangle>{new Rectangle(250,330,150,100)}),
                none=VisualTools.RecordingFrameFullyCovered(bounds,new List<Rectangle>())}));return 0;
        }
        if(args[0].StartsWith("wgc_bounds")) {
            Rectangle raw=new Rectangle(-208,-8,176,136),visible=new Rectangle(-200,0,160,120);
            Size size=args[0]=="wgc_bounds_raw"?raw.Size:args[0]=="wgc_bounds_visible"?visible.Size:new Size(159,120);
            try {var value=VisualTools.GraphicsWindowCapture.SourceBounds(size,raw,visible);
                Console.WriteLine(json.Serialize(new{x=value.X,y=value.Y,width=value.Width,height=value.Height}));}
            catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new{code=error.Message}));}return 0;
        }
        if(args[0].StartsWith("wgc_rows")) {
            const int width=24,height=12,pitch=112;byte[] bytes=new byte[pitch*height];
            for(int y=0;y<height;y++)for(int x=0;x<width;x++){int at=y*pitch+x*4;bytes[at]=(byte)x;bytes[at+1]=(byte)y;bytes[at+2]=(byte)(x+y);bytes[at+3]=0;}
            IntPtr memory=Marshal.AllocHGlobal(bytes.Length);
            try {Marshal.Copy(bytes,0,memory,bytes.Length);
                try {using(var image=VisualTools.GraphicsWindowCapture.ReadRows(memory,args[0]=="wgc_rows_invalid"?95u:pitch,new Size(width,height))) {
                    bool exact=true;for(int y=0;y<height;y++)for(int x=0;x<width;x++){Color pixel=image.GetPixel(x,y);exact &= pixel.A==255&&pixel.R==x+y&&pixel.G==y&&pixel.B==x;}
                    Console.WriteLine(json.Serialize(new{exact=exact,width=image.Width,height=image.Height}));}}
                catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new{code=error.Message}));}
            }finally{Marshal.FreeHGlobal(memory);}return 0;
        }
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
        if(args[0]=="context_blank_input") {
            using(var frame=new Bitmap(1000,600)) {
                using(var g=Graphics.FromImage(frame)) using(var font=new Font(FontFamily.GenericSansSerif,24,FontStyle.Bold)) {
                    g.Clear(Color.FromArgb(232,240,250));
                    foreach(int x in new[]{80,540}) {g.FillRectangle(Brushes.White,x,190,350,100);g.DrawRectangle(Pens.SlateGray,x,190,350,100);}
                    g.DrawString("Family",font,Brushes.DarkBlue,80,130);
                    g.DrawString("Search",font,Brushes.DarkBlue,540,130);
                }
                var selected=new Rectangle(523,192,384,96);var point=new Point(715,240);
                var result=VisualTools.AuthorImage(frame,selected,point,new List<Rectangle>());
                var region=(Dictionary<string,object>)result["selection"];var anchor=(Dictionary<string,object>)result["anchor"];
                var rect=new Rectangle(Convert.ToInt32(region["x"]),Convert.ToInt32(region["y"]),Convert.ToInt32(region["width"]),Convert.ToInt32(region["height"]));
                Console.WriteLine(json.Serialize(new {region=region,width=result["width"],height=result["height"],
                    label_included=rect.Contains(new Rectangle(540,130,145,40)),
                    upper_and_lower_border=rect.Top<=190&&rect.Bottom>=290,
                    source_unique=((Dictionary<string,object>)result["capture_diagnostic"])["source_frame_unique"],
                    action_x=rect.X+Convert.ToDouble(anchor["x"])*rect.Width,
                    action_y=rect.Y+Convert.ToDouble(anchor["y"])*rect.Height}));return 0;
            }
        }
        if(args[0]=="context_unique_input") {
            using(var frame=new Bitmap(700,420)) {
                using(var g=Graphics.FromImage(frame)) using(var font=new Font(FontFamily.GenericSansSerif,24,FontStyle.Bold)) {
                    g.Clear(Color.FromArgb(232,240,250));
                    g.DrawString("Device",font,Brushes.DarkBlue,190,80);
                    g.FillRectangle(Brushes.White,190,150,320,96);g.DrawRectangle(Pens.SlateGray,190,150,320,96);
                    g.DrawString("D200",font,Brushes.Black,210,177);
                    g.FillPolygon(Brushes.DarkBlue,new Point[]{new Point(478,190),new Point(496,190),new Point(487,204)});
                }
                var selected=new Rectangle(158,150,384,96);var point=new Point(350,198);
                var manual=VisualTools.AuthorImage(frame,selected,point,new List<Rectangle>());
                var recorded=VisualTools.AuthorImage(frame,selected,point,new List<Rectangle>(),true);
                var region=(Dictionary<string,object>)recorded["selection"];var anchor=(Dictionary<string,object>)recorded["anchor"];
                var rect=new Rectangle(Convert.ToInt32(region["x"]),Convert.ToInt32(region["y"]),Convert.ToInt32(region["width"]),Convert.ToInt32(region["height"]));
                Console.WriteLine(json.Serialize(new {manual=manual["selection"],manual_diagnostic=manual["capture_diagnostic"],recorded=region,
                    label_included=rect.Contains(new Rectangle(190,80,135,40)),diagnostic=recorded["capture_diagnostic"],
                    action_x=rect.X+Convert.ToDouble(anchor["x"])*rect.Width,
                    action_y=rect.Y+Convert.ToDouble(anchor["y"])*rect.Height}));return 0;
            }
        }
        if(args[0]=="context_similar_values") {
            using(var frame=new Bitmap(640,440)) {
                using(var g=Graphics.FromImage(frame)) using(var font=new Font(FontFamily.GenericSansSerif,28,FontStyle.Bold)) {
                    g.Clear(Color.FromArgb(232,240,250));g.DrawString("Device",font,Brushes.DarkBlue,80,24);
                    g.FillRectangle(Brushes.White,80,80,400,288);g.DrawRectangle(Pens.SlateGray,80,80,400,288);
                    for(int row=0;row<3;row++) {g.DrawString("D"+(row+1)+"00",font,Brushes.Black,104,92+row*90);g.DrawLine(Pens.LightGray,80,164+row*90,480,164+row*90);}
                }
                var point=new Point(280,218);var recorded=VisualTools.AuthorImage(frame,new Rectangle(88,170,384,96),point,new List<Rectangle>(),true);
                var region=(Dictionary<string,object>)recorded["selection"];var anchor=(Dictionary<string,object>)recorded["anchor"];
                var request=new Dictionary<string,object>(recorded);
                using(var stream=new System.IO.MemoryStream()){frame.Save(stream,ImageFormat.Png);request["screenshot_png"]=Convert.ToBase64String(stream.ToArray());}
                var match=(Dictionary<string,object>)typeof(VisualTools).GetMethod("Match",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{request});
                var matchRect=match.ContainsKey("rect")?(Dictionary<string,object>)match["rect"]:null;
                Console.WriteLine(json.Serialize(new {selection=region,match=match,action_x=matchRect==null?-1:Convert.ToDouble(matchRect["x"])+Convert.ToDouble(anchor["x"])*Convert.ToDouble(matchRect["width"]),
                    action_y=matchRect==null?-1:Convert.ToDouble(matchRect["y"])+Convert.ToDouble(anchor["y"])*Convert.ToDouble(matchRect["height"])}));return 0;
            }
        }
        if(args[0]=="authoring_large_frame") {
            using(var frame=new Bitmap(2880,1704)) {
                using(var g=Graphics.FromImage(frame))g.Clear(Color.FromArgb(200,200,200));
                var random=new Random(8127);var selected=new Rectangle(800,500,384,192);
                for(int y=selected.Top;y<selected.Bottom;y++)for(int x=selected.Left;x<selected.Right;x++) {
                    int value=160+random.Next(81);frame.SetPixel(x,y,Color.FromArgb(value,value,value));
                }
                var watch=System.Diagnostics.Stopwatch.StartNew();
                try { var result=VisualTools.AuthorImage(frame,new Rectangle(800,580,384,96),new Point(992,628),new List<Rectangle>(),true);
                    Console.WriteLine(json.Serialize(new {accepted=true,elapsed_ms=watch.ElapsedMilliseconds,selection=result["selection"],anchor=result["anchor"]})); }
                catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new {accepted=false,elapsed_ms=watch.ElapsedMilliseconds,code=error.Message}));}
                return 0;
            }
        }
        if(args[0]=="authoring_sample_equivalence") {
            var flags=BindingFlags.Static|BindingFlags.NonPublic;var instance=BindingFlags.Instance|BindingFlags.NonPublic;
            var pixelsType=typeof(VisualTools).GetNestedType("Pixels",BindingFlags.NonPublic);
            var samplesType=typeof(VisualTools).GetNestedType("AuthoringSamples",BindingFlags.NonPublic);
            var score=typeof(VisualTools).GetMethod("Score",flags);var features=typeof(VisualTools).GetMethod("Features",flags);
            var featureScore=typeof(VisualTools).GetMethod("FeatureScore",flags);var same=typeof(VisualTools).GetMethod("SameAuthoringOrigin",flags);
            int checks=0;bool equivalent=true;
            using(var pattern=new Bitmap(139,137))using(var screen=new Bitmap(160,160)) {
                var random=new Random(431);using(var g=Graphics.FromImage(screen))g.Clear(Color.White);
                for(int y=0;y<pattern.Height;y++)for(int x=0;x<pattern.Width;x++) {
                    int shade=random.Next(256);pattern.SetPixel(x,y,Color.FromArgb(shade,random.Next(256),random.Next(256)));screen.SetPixel(x+7,y+9,pattern.GetPixel(x,y));
                }
                object image=Activator.CreateInstance(pixelsType,instance,null,new object[]{screen},null),template=Activator.CreateInstance(pixelsType,instance,null,new object[]{pattern},null);
                object selected=features.Invoke(null,new object[]{template});
                foreach(bool feature in new[]{false,true}) {
                    object prepared=Activator.CreateInstance(samplesType,instance,null,new object[]{image,template,feature?selected:null},null);
                    var atLeast=samplesType.GetMethod("AtLeast",instance);
                    foreach(var point in new[]{new Point(0,0),new Point(7,9),new Point(8,9),new Point(21,23)}) {
                        double expected=Convert.ToDouble(feature?featureScore.Invoke(null,new object[]{image,template,selected,point.X,point.Y,0.0}):score.Invoke(null,new object[]{image,template,point.X,point.Y,false}));
                        int origin=point.Y*Convert.ToInt32(pixelsType.GetField("Stride",instance).GetValue(image))+point.X*4;
                        foreach(double floor in new[]{expected-1e-12,expected,expected+1e-12,.79,.94,.97}) {
                            bool actual=(bool)atLeast.Invoke(prepared,new object[]{image,origin,floor});equivalent &= actual==(expected>=floor);checks++;
                        }
                    }
                }
            }
            using(var pattern=new Bitmap(199,199))using(var screen=new Bitmap(199,199)) {
                using(var g=Graphics.FromImage(pattern))g.Clear(Color.Black);using(var g=Graphics.FromImage(screen))g.Clear(Color.White);
                object image=Activator.CreateInstance(pixelsType,instance,null,new object[]{screen},null),template=Activator.CreateInstance(pixelsType,instance,null,new object[]{pattern},null);
                object prepared=Activator.CreateInstance(samplesType,instance,null,new object[]{image,template,null},null);
                foreach(double floor in new[]{0.0,1e-12,.94}) {bool actual=(bool)samplesType.GetMethod("AtLeast",instance).Invoke(prepared,new object[]{image,0,floor});equivalent &= actual==(0.0>=floor);checks++;}
            }
            Console.WriteLine(json.Serialize(new{equivalent=equivalent,checks=checks}));return 0;
        }
        if(args[0]=="authoring_moment_equivalence") {
            var flags=BindingFlags.Static|BindingFlags.NonPublic;var instance=BindingFlags.Instance|BindingFlags.NonPublic;
            var pixelsType=typeof(VisualTools).GetNestedType("Pixels",BindingFlags.NonPublic);
            var samplesType=typeof(VisualTools).GetNestedType("AuthoringSamples",BindingFlags.NonPublic);
            var indexType=typeof(VisualTools).GetNestedType("AuthoringMomentIndex",BindingFlags.NonPublic);
            var score=typeof(VisualTools).GetMethod("Score",flags);
            int checks=0;bool equivalent=true,reused=true,released=true;
            using(var screen=new Bitmap(430,350)) {
                var random=new Random(9282);
                for(int y=0;y<screen.Height;y++)for(int x=0;x<screen.Width;x++)screen.SetPixel(x,y,Color.FromArgb(196+random.Next(17),196+random.Next(17),196+random.Next(17)));
                object image=Activator.CreateInstance(pixelsType,instance,null,new object[]{screen},null);
                object index=Activator.CreateInstance(indexType,instance,null,new object[]{image},null),firstSums=null,firstSquares=null;
                foreach(var size in new[]{new Size(31,27),new Size(201,303),new Size(384,192)})using(var pattern=screen.Clone(new Rectangle(7,9,size.Width,size.Height),PixelFormat.Format32bppArgb)) {
                    object template=Activator.CreateInstance(pixelsType,instance,null,new object[]{pattern},null);
                    object prepared=Activator.CreateInstance(samplesType,instance,null,new object[]{image,template,null},null);
                    foreach(var point in new[]{new Point(0,0),new Point(7,9),new Point(8,9),new Point(20,20)}) {
                        double expected=Convert.ToDouble(score.Invoke(null,new object[]{image,template,point.X,point.Y,false}));
                        int origin=point.Y*Convert.ToInt32(pixelsType.GetField("Stride",instance).GetValue(image))+point.X*4;
                        foreach(double floor in new[]{expected-1e-12,expected,expected+1e-12,.94,.97}) {
                            bool indexed=(bool)indexType.GetMethod("Prepare",instance).Invoke(index,new object[]{prepared,floor,System.Diagnostics.Stopwatch.StartNew()});
                            bool possible=(bool)indexType.GetMethod("MayReach",instance).Invoke(index,new object[]{point.X,point.Y});
                            bool actual=possible&&(bool)samplesType.GetMethod("AtLeast",instance).Invoke(prepared,new object[]{image,origin,floor});
                            equivalent &= indexed&&actual==(expected>=floor);checks++;
                            var sums=indexType.GetField("sums",instance).GetValue(index);var squares=indexType.GetField("squares",instance).GetValue(index);
                            if(firstSums==null){firstSums=sums;firstSquares=squares;}else reused &= Object.ReferenceEquals(firstSums,sums)&&Object.ReferenceEquals(firstSquares,squares);
                        }
                    }
                }
                ((IDisposable)index).Dispose();released &= indexType.GetField("sums",instance).GetValue(index)==null&&indexType.GetField("squares",instance).GetValue(index)==null;
                var huge=System.Runtime.Serialization.FormatterServices.GetUninitializedObject(pixelsType);pixelsType.GetField("W",instance).SetValue(huge,4096);pixelsType.GetField("H",instance).SetValue(huge,4096);
                object limited=Activator.CreateInstance(indexType,instance,null,new object[]{huge},null);
                bool cap=!(bool)indexType.GetMethod("Prepare",instance).Invoke(limited,new object[]{null,.97,System.Diagnostics.Stopwatch.StartNew()});
                cap &= indexType.GetField("sums",instance).GetValue(limited)==null&&indexType.GetField("squares",instance).GetValue(limited)==null;((IDisposable)limited).Dispose();
                Console.WriteLine(json.Serialize(new{equivalent=equivalent,checks=checks,buffers_reused=reused,buffers_released=released,cap_no_allocation=cap}));return 0;
            }
        }
        if(args[0]=="authoring_near_duplicate") {
            using(var frame=new Bitmap(900,400))using(var tile=new Bitmap(384,192)) {
                var random=new Random(397);for(int y=0;y<tile.Height;y++)for(int x=0;x<tile.Width;x++){int value=160+random.Next(81);tile.SetPixel(x,y,Color.FromArgb(value,value,value));}
                using(var g=Graphics.FromImage(frame)){g.Clear(Color.FromArgb(200,200,200));g.DrawImageUnscaled(tile,20,100);g.DrawImageUnscaled(tile,470,100);}
                var changed=frame.GetPixel(475,105);frame.SetPixel(475,105,Color.FromArgb(changed.R+1,changed.G+1,changed.B+1));
                var pixelsType=typeof(VisualTools).GetNestedType("Pixels",BindingFlags.NonPublic);var indexType=typeof(VisualTools).GetNestedType("AuthoringMomentIndex",BindingFlags.NonPublic);
                var instance=BindingFlags.Instance|BindingFlags.NonPublic;object screen=Activator.CreateInstance(pixelsType,instance,null,new object[]{frame},null);
                object index=Activator.CreateInstance(indexType,instance,null,new object[]{screen},null);
                bool unique=(bool)typeof(VisualTools).GetMethod("DistinctInFrame",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{screen,tile,new Rectangle(20,100,384,192),System.Diagnostics.Stopwatch.StartNew(),index});
                ((IDisposable)index).Dispose();Console.WriteLine(json.Serialize(new{unique=unique}));return 0;
            }
        }
        if(args[0].StartsWith("author_")) {
            using(var frame=new Bitmap(620,400)) {
                using(var g=Graphics.FromImage(frame)) {
                    g.Clear(Color.FromArgb(45,45,45));
                    for(int row=0;row<3;row++) {
                        int y=70+row*120; g.FillEllipse(Brushes.DimGray,472,y-18,36,36);
                        g.FillPolygon(Brushes.White,new Point[]{new Point(484,y-11),new Point(484,y+11),new Point(503,y)});
                        if(args[0]!="author_duplicate" && args[0]!="author_duplicate_recording") using(var brush=new SolidBrush(row==0?Color.Cyan:row==1?Color.Red:Color.Lime)) {
                            g.FillRectangle(brush,40,y-14,130,28);
                            using(var font=new Font(FontFamily.GenericSansSerif,18,FontStyle.Bold)) g.DrawString("INSTANCE " + (row==0?"ALPHA":row==1?"BETA":"GAMMA"),font,Brushes.White,40,y-14);
                        }
                    }
                }
                var blocked=new List<Rectangle>();
                if(args[0]=="author_context_blocked" || args[0]=="author_layered_context") blocked.Add(new Rectangle(545,0,75,400));
                if(args[0]=="author_anchor_blocked" || args[0]=="author_layered_anchor") blocked.Add(new Rectangle(480,60,30,30));
                if(args[0]=="author_layered_full") blocked.Add(new Rectangle(0,0,620,400));
                if(args[0]=="author_layered_context") blocked.Add(new Rectangle(0,0,20,400));
                try {
                    bool rendered=false,excludesLayered=false;
                    using(var captured=args[0].StartsWith("author_layered")?VisualTools.RecordingLayeredCapture(
                        delegate{throw new InvalidOperationException("target_render_incomplete");},
                        delegate{throw new InvalidOperationException("target_render_waiting");},
                        delegate{return (Bitmap)frame.Clone();},out rendered,out excludesLayered):(Bitmap)frame.Clone()) {
                    var value=VisualTools.AuthorImage(captured,new Rectangle(468,46,48,48),new Point(492,70),blocked,args[0]=="author_duplicate_recording");
                    var selected=(Dictionary<string,object>)value["selection"];var anchor=(Dictionary<string,object>)value["anchor"];
                    Console.WriteLine(json.Serialize(new {accepted=true,selection=selected,diagnostic=value["capture_diagnostic"],
                        rendered=rendered,excludes_layered=excludesLayered,
                        action_x=Convert.ToDouble(selected["x"])+Convert.ToDouble(anchor["x"])*Convert.ToDouble(selected["width"]),
                        action_y=Convert.ToDouble(selected["y"])+Convert.ToDouble(anchor["y"])*Convert.ToDouble(selected["height"])}));
                    }
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
        if(args[0]=="layered") {
            int renderCalls=0,screenCalls=0;bool rendered=false,excludesLayered=false;
            try {
                using(var image=VisualTools.RecordingLayeredCapture(delegate {
                    if(args[1]!="ok") throw new InvalidOperationException(args[1]);
                    return new Bitmap(160,120);
                },delegate {
                    renderCalls++;if(args[2]!="ok")throw new InvalidOperationException(args[2]);
                    return new Bitmap(160,120);
                },delegate {
                    screenCalls++;if(args[3]!="ok")throw new InvalidOperationException(args[3]);
                    return new Bitmap(160,120);
                },out rendered,out excludesLayered))Console.WriteLine(json.Serialize(new {render_calls=renderCalls,screen_calls=screenCalls,
                    rendered=rendered,excludes_layered=excludesLayered,width=image.Width}));
            }catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new {code=error.Message,render_calls=renderCalls,screen_calls=screenCalls}));}
            return 0;
        }
        if(args[0].StartsWith("surface_")) {
            int renderCalls=0; bool rendered=false;
            try {
                using(var image=VisualTools.RecordingSurfaceCapture(delegate {
                    if(args[0]=="surface_ok") return new Bitmap(160,120);
                    string failure=args[0]=="surface_changed"?"target_changed":args[0]=="surface_foreground"?"target_requires_foreground":
                        args[0]=="surface_missing"?"target_unavailable":args[0]=="surface_unavailable"?"window_surface_unavailable":"target_render_incomplete";
                    throw new InvalidOperationException(failure);
                }, delegate {
                    renderCalls++;
                    if(args[0]=="surface_waiting") throw new InvalidOperationException("target_render_waiting");
                    using(var full=new Bitmap(160,120)) {
                        using(var g=Graphics.FromImage(full))g.Clear(Color.Black);
                        if(args[0]!="surface_blank") for(int y=0;y<120;y++)for(int x=0;x<160;x++)full.SetPixel(x,y,Color.FromArgb((x*13)%256,(y*17)%256,((x+y)*11)%256));
                        return VisualTools.ValidateRenderedFrame(full,new Rectangle(0,0,160,120),new Rectangle(0,0,160,120));
                    }
                },out rendered)) Console.WriteLine(json.Serialize(new {render_calls=renderCalls,rendered=rendered,width=image.Width}));
            }catch(InvalidOperationException error){Console.WriteLine(json.Serialize(new {code=error.Message,render_calls=renderCalls}));}
            return 0;
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

    def test_persistent_capture_latest_only_leases_survive_replace_and_stop(self):
        self.assertEqual(self.call("capture_slot"), {"old_stable": True, "latest": True,
            "two_frames": True, "stopped_empty": True, "held_after_stop": True,
            "late_rejected": True, "retained": 0})

    def test_static_scene_requests_actual_new_capture_without_relabeling_pixel_time(self):
        self.assertEqual(self.call("capture_refresh"), {"cold": False, "fresh": False,
            "static_scene": True, "request_recent": False, "awaiting_early": False,
            "awaiting_retry": True, "future": False, "old_pixels_still_stale": False,
            "old_pixels_before_input": False})

    def test_timestamp_diagnostics_distinguish_future_old_and_invalid_without_accepting_them(self):
        self.assertEqual(self.call("capture_time_reasons"), ["target_changed:capture_time_invalid",
            "target_changed:capture_time_future", "target_changed:capture_time_stale"])

    def test_future_compositor_frame_waits_for_real_clock_and_preserves_acquisition_input_bound(self):
        self.assertEqual(self.call("future_capture_wait"), {"delays": 7, "lead_ms": 7,
            "source_ms": 7, "input_before_acquisition": True, "input_during_dequeue_or_wait": False,
            "source_unchanged": True, "unreasonable_rejected": True, "frozen_rejected": True,
            "frozen_delays": 64})

    def test_new_request_first_frame_accepts_static_render_without_promoting_old_pool_or_input_epoch(self):
        self.assertEqual(self.call("capture_request_epoch"), {"static_source": True,
            "source_preserved": True, "request_preserved": True, "received_preserved": True,
            "intervening_input": False, "same_tick_new_epoch": False, "expired_request": False,
            "no_frame": False, "first": True, "second": False, "new_session_first": True,
            "old_tag_still_request": True, "historical_render_age_ms": 60000})

    def test_recording_retains_only_epoch_bound_requested_frames_without_loosening_ordinary_capture(self):
        self.assertEqual(self.call("recording_request_retention"), {"recording": True,
            "generic": False, "exact_boundary": True, "expired": False,
            "input_after_request": False, "changed_epoch": False, "one_shot": False,
            "legacy": False, "future": False, "hook": None, "source_preserved": True,
            "request_preserved": True, "received_preserved": True})

    def test_transient_capture_wait_preserves_only_current_epoch_cache_and_hook_restores_null_frame(self):
        result = self.call("recorder_waiting_cache")
        diagnostic = result.pop("diagnostic")
        self.assertEqual(result, {"restored_same_target_null_frame": True,
            "pending_kept_original_pixels": True, "changed_epoch_cleared": True,
            "changed_epoch_not_restored": True, "scope_failures_cleared": True,
            "stale_not_restored": True, "retained": 0})
        self.assertEqual(diagnostic, {"recording_input_epoch": 7, "recording_active_frame_epoch": 7,
            "recording_cached_frame_epoch": 7, "recording_cached_frame_age_ms": 400,
            "recording_cached_time_epoch_valid": True})

    def test_persistent_capture_ui_handoff_does_not_wait_for_stalled_producer(self):
        result = self.call("capture_slot_nonblocking")
        self.assertTrue(result["available"])
        self.assertLess(result["elapsed_ms"], 50)
        self.assertEqual(result["retained"], 0)

    def test_persistent_capture_rejects_other_lifetime_geometry_stale_or_preinput_pixels(self):
        self.assertEqual(self.call("capture_pump_identity"), {"exact": True,
            "other_owner": False, "moved": False, "pre_input_rejected": True,
            "acquisition_rejected": True, "stale_rejected": True, "stopped": False, "retained": 0})

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

    def test_wgc_accepts_exact_raw_or_dwm_dimensions_without_coordinate_scaling(self):
        self.assertEqual(self.call("wgc_bounds_raw"), {"x": -208, "y": -8, "width": 176, "height": 136})
        self.assertEqual(self.call("wgc_bounds_visible"), {"x": -200, "y": 0, "width": 160, "height": 120})
        self.assertEqual(self.call("wgc_bounds_wrong"), {"code": "target_capture_size_invalid"})

    def test_wgc_gpu_row_padding_and_zero_alpha_preserve_exact_rgb_pixels(self):
        self.assertEqual(self.call("wgc_rows"), {"exact": True, "width": 24, "height": 12})
        self.assertEqual(self.call("wgc_rows_invalid"), {"code": "target_capture_size_invalid"})

    def test_recording_preparation_waits_only_for_fully_covered_screen_frames(self):
        self.assertEqual(self.call("full_coverage"), {"one": True, "joined": True, "gap": False, "toolbar": False, "none": False})

    def test_recording_backend_diagnostic_is_bounded_and_tied_to_exact_target(self):
        result = self.call("backend_diagnostic")
        self.assertEqual(result["pending"], {"capture_backend_stage": "map_texture", "capture_backend_hresult": "887A000A", "capture_backend_pending": True})
        self.assertEqual(result["completed"], {"capture_backend_stage": "map_texture", "capture_backend_hresult": "887A000A", "capture_backend_pending": False})
        self.assertEqual(result["other"], {})

    def test_renderer_freshness_uses_compositor_frame_time_not_worker_start_or_completion(self):
        self.assertEqual(self.call("capture_freshness"), {"qpc_age_ms": 10, "slow_startup_fresh": True,
            "recent_completion_old_pixels": False, "future_fresh": False, "future_qpc_rejected": True, "old_qpc_rejected": True,
            "prior_input": True, "newer_input": False})

    def test_capture_failures_distinguish_missing_stale_input_geometry_and_scope(self):
        self.assertEqual(self.call("frame_failure"), {"missing": "frame_missing", "target": "frame_target_changed",
            "stale": "frame_stale", "input": "frame_predates_input", "geometry": "frame_geometry_changed",
            "outside": "point_outside_target", "usable": None})

    def test_white_and_black_unrendered_frames_are_not_templates(self):
        for mode in ("white","black"):
            with self.subTest(mode=mode): self.assertEqual(self.call(mode), {"code":"target_render_incomplete"})

    def test_partially_rendered_sentinel_frame_is_rejected(self):
        self.assertEqual(self.call("sentinel"), {"code":"target_render_incomplete"})

    def test_surface_failure_recovers_only_with_validated_target_render(self):
        self.assertEqual(self.call("surface_ok"), {"render_calls": 0, "rendered": False, "width": 160})
        for mode in ("surface_black", "surface_unavailable"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"render_calls": 1, "rendered": True, "width": 160})
        for mode, code in (("surface_blank", "target_render_incomplete"), ("surface_waiting", "target_render_waiting")):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"code": code, "render_calls": 1})

    def test_surface_scope_or_foreground_change_never_tries_another_capture(self):
        for mode, code in (("surface_changed", "target_changed"), ("surface_foreground", "target_requires_foreground"),
                           ("surface_missing", "target_unavailable")):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"code": code, "render_calls": 0})

    def test_layered_capture_retains_actual_capture_provenance(self):
        self.assertEqual(self.call("layered", "ok", "ok", "ok"), {"render_calls": 0, "screen_calls": 0,
            "rendered": False, "excludes_layered": True, "width": 160})
        self.assertEqual(self.call("layered", "target_render_incomplete", "ok", "ok"), {"render_calls": 1, "screen_calls": 0,
            "rendered": True, "excludes_layered": False, "width": 160})

    def test_layered_capture_availability_failure_uses_screen_with_all_masks(self):
        for code in ("target_render_waiting", "target_capture_busy", "target_render_unavailable",
                     "target_render_incomplete", "window_surface_unavailable"):
            with self.subTest(code=code):
                self.assertEqual(self.call("layered", "target_render_incomplete", code, "ok"),
                    {"render_calls": 1, "screen_calls": 1, "rendered": False, "excludes_layered": False, "width": 160})

    def test_layered_capture_never_recovers_scope_geometry_or_unknown_failures(self):
        for code in ("target_changed", "target_requires_foreground", "target_unavailable", "target_capture_size_invalid", "unexpected"):
            with self.subTest(code=code, stage="surface"):
                self.assertEqual(self.call("layered", code, "ok", "ok"), {"code": code, "render_calls": 0, "screen_calls": 0})
            with self.subTest(code=code, stage="renderer"):
                self.assertEqual(self.call("layered", "target_render_incomplete", code, "ok"),
                    {"code": code, "render_calls": 1, "screen_calls": 0})
        self.assertEqual(self.call("layered", "target_render_incomplete", "target_render_waiting", "target_changed"),
            {"code": "target_changed", "render_calls": 1, "screen_calls": 1})

    def test_screen_fallback_clips_both_occluder_masks_without_moving_anchor(self):
        result = self.call("author_layered_context")
        self.assertTrue(result["accepted"], result)
        self.assertFalse(result["rendered"])
        self.assertFalse(result["excludes_layered"])
        self.assertEqual((result["action_x"], result["action_y"]), (492, 70))
        self.assertGreaterEqual(result["selection"]["x"], 20)
        self.assertLessEqual(result["selection"]["x"] + result["selection"]["width"], 545)
        for mode in ("author_layered_anchor", "author_layered_full"):
            with self.subTest(mode=mode):
                self.assertEqual(self.call(mode), {"accepted": False, "code": "image_occluded_requires_selection"})

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

    def test_blank_input_retains_label_above_and_borders_without_long_compressed_strip(self):
        result = self.call("context_blank_input")
        self.assertTrue(result["label_included"], result)
        self.assertTrue(result["upper_and_lower_border"], result)
        self.assertTrue(result["source_unique"])
        self.assertEqual((result["action_x"], result["action_y"]), (715, 240))
        self.assertLessEqual(result["region"]["width"], 640)
        self.assertGreaterEqual(result["height"], 160)
        self.assertLessEqual(result["width"], 512)

    def test_recorder_prefers_label_context_even_when_manual_small_selection_is_unique(self):
        result = self.call("context_unique_input")
        self.assertEqual(result["manual"], {"x": 158, "y": 150, "width": 384, "height": 96})
        self.assertEqual(result["manual_diagnostic"]["candidate_count"], 1)
        self.assertFalse(result["manual_diagnostic"]["context_expanded"])
        self.assertTrue(result["label_included"], result)
        self.assertTrue(result["diagnostic"]["source_frame_unique"])
        self.assertTrue(result["diagnostic"]["context_expanded"])
        self.assertEqual((result["action_x"], result["action_y"]), (350, 198))

    def test_recorded_context_matches_d200_without_selecting_adjacent_d100_or_d300(self):
        result = self.call("context_similar_values")
        self.assertEqual(result["match"]["status"], "matched", result)
        self.assertAlmostEqual(result["action_x"], 280, delta=1)
        self.assertAlmostEqual(result["action_y"], 218, delta=1)
        self.assertGreaterEqual(result["selection"]["height"], 192)

    def test_recorder_context_does_not_resolve_indistinguishable_rows_by_position(self):
        self.assertEqual(self.call("author_duplicate_recording"), {"accepted": False, "code": "image_ambiguous"})

    def test_large_frame_context_search_finishes_inside_unchanged_authoring_deadline(self):
        result = self.call("authoring_large_frame")
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["selection"], {"x": 800, "y": 500, "width": 384, "height": 192})
        self.assertEqual(result["anchor"], {"x": .5, "y": 2/3})
        self.assertLess(result["elapsed_ms"], 8000)

    def test_authoring_cached_samples_match_original_rms_at_threshold_neighbors_and_large_sse(self):
        self.assertEqual(self.call("authoring_sample_equivalence"), {"equivalent": True, "checks": 51})

    def test_authoring_moment_bound_matches_original_score_and_reuses_bounded_buffers(self):
        self.assertEqual(self.call("authoring_moment_equivalence"), {"equivalent": True, "checks": 60,
            "buffers_reused": True, "buffers_released": True, "cap_no_allocation": True})

    def test_authoring_moment_bound_preserves_one_pixel_near_duplicate_ambiguity(self):
        self.assertEqual(self.call("authoring_near_duplicate"), {"unique": False})

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
