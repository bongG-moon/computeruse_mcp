// Local, same-user/session stdio bridge. Elevation uses Windows runas/UAC.
// No service, scheduled task, TCP listener, policy change, or stored credential.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

internal static class AdministratorBridge
{
    const int MaxFrame = 65536;
    const int ConnectMilliseconds = 90000;
    static readonly string BaseDirectory = AppDomain.CurrentDomain.BaseDirectory;
    static readonly string Self = Process.GetCurrentProcess().MainModule.FileName;
    static readonly Encoding Utf8 = new UTF8Encoding(false, true);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetNamedPipeClientProcessId(IntPtr pipe, out uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetNamedPipeServerProcessId(IntPtr pipe, out uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool ProcessIdToSessionId(uint pid, out uint session);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes { internal int Length; internal IntPtr Descriptor; internal int Inherit; }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateNamedPipe(string name, uint openMode, uint pipeMode, uint instances, uint output, uint input, uint timeout, ref SecurityAttributes security);

    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            var options = Parse(args);
            if (options.ContainsKey("self-test"))
            {
                WriteConsole(Console.OpenStandardOutput(), Utf8.GetBytes("{\"ok\":true,\"administrator\":" + (IsAdministrator() ? "true" : "false") + ",\"transport\":\"authenticated_local_named_pipe\"}\n"));
                return 0;
            }
            if (!Environment.UserInteractive || Session((uint)Process.GetCurrentProcess().Id) == 0)
                throw new InvalidOperationException("로그인한 사용자의 대화형 Windows 세션에서 실행해 주세요.");
            string config = Require(options, "config");
            if (!Path.IsPathRooted(config) || config.StartsWith(@"\\") || !File.Exists(config))
                throw new InvalidOperationException("이 PC의 기존 설정 파일 전체 경로를 지정해 주세요.");
            config = Path.GetFullPath(config);
            return options.ContainsKey("worker") ? Worker(options, config) : Connect(options, config);
        }
        catch (Win32Exception error)
        {
            Error(error.NativeErrorCode == 1223 ? "관리자 권한 요청이 취소되었습니다. MCP와 Driver를 시작하지 않았습니다."
                : "관리자 연결을 시작하지 못했습니다. Windows 오류: " + error.NativeErrorCode);
            return error.NativeErrorCode == 1223 ? 1223 : 1;
        }
        catch (Exception error)
        {
            Error(error is TimeoutException ? "관리자 연결 시간이 초과되었습니다. UAC 승인 여부를 확인한 뒤 다시 연결하세요."
                : "관리자 연결 실패: " + error.Message);
            return 1;
        }
    }

    static Dictionary<string, string> Parse(string[] args)
    {
        var result = new Dictionary<string, string>();
        var values = new HashSet<string>(new string[] { "config", "pipe", "parent", "nonce", "sid", "session", "require-admin" });
        var switches = new HashSet<string>(new string[] { "worker", "normal", "self-test" });
        for (int i=0; i<args.Length; i++)
        {
            if (!args[i].StartsWith("--")) throw new ArgumentException("지원하지 않는 실행 인자입니다.");
            string key = args[i].Substring(2);
            if (result.ContainsKey(key) || (!values.Contains(key) && !switches.Contains(key)))
                throw new ArgumentException("지원하지 않거나 중복된 실행 인자입니다.");
            if (switches.Contains(key)) result[key] = "true";
            else
            {
                if (++i >= args.Length || String.IsNullOrEmpty(args[i])) throw new ArgumentException("실행 인자 값이 없습니다.");
                result[key] = args[i];
            }
        }
        if (result.ContainsKey("self-test") && result.Count != 1) throw new ArgumentException("확인 명령은 다른 인자와 함께 사용할 수 없습니다.");
        if (!result.ContainsKey("worker") && !result.ContainsKey("self-test"))
            foreach (string key in result.Keys)
                if (key != "config" && key != "normal") throw new ArgumentException("내부 연결 인자를 외부에서 사용할 수 없습니다.");
        return result;
    }

    static string Require(Dictionary<string,string> options, string key)
    {
        string value;
        if (!options.TryGetValue(key, out value)) throw new ArgumentException("필수 실행 인자가 없습니다: " + key);
        return value;
    }

    static bool IsAdministrator()
    {
        using (var identity = WindowsIdentity.GetCurrent())
            return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
    }
    static string CurrentSid() { using (var identity = WindowsIdentity.GetCurrent()) return identity.User.Value; }
    static uint Session(uint pid)
    {
        uint session;
        if (!ProcessIdToSessionId(pid, out session)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return session;
    }
    static bool SameProcess(uint pid)
    {
        if (Session(pid) != Session((uint)Process.GetCurrentProcess().Id)) return false;
        IntPtr process = OpenProcess(0x1000, false, pid), token = IntPtr.Zero;
        if (process == IntPtr.Zero) return false;
        try
        {
            if (!OpenProcessToken(process, 8, out token)) return false;
            using (var identity = new WindowsIdentity(token))
                if (identity.User.Value != CurrentSid()) return false;
            uint size = 32768; var path = new StringBuilder((int)size);
            return QueryFullProcessImageName(process, 0, path, ref size)
                && String.Equals(Path.GetFullPath(path.ToString()), Path.GetFullPath(Self), StringComparison.OrdinalIgnoreCase);
        }
        finally { if (token != IntPtr.Zero) CloseHandle(token); CloseHandle(process); }
    }

    static int Connect(Dictionary<string,string> options, string config)
    {
        bool administrator = !options.ContainsKey("normal");
        string sid = CurrentSid(), nonce = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
        string name = "CompanyComputerUse." + Guid.NewGuid().ToString("N");
        int parent = Process.GetCurrentProcess().Id;
        uint session = Session((uint)parent);
        var security = new PipeSecurity();
        security.SetAccessRuleProtection(true, false);
        security.SetOwner(new SecurityIdentifier(sid));
        security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(sid), PipeAccessRights.FullControl, AccessControlType.Allow));
        using (var pipe = LocalPipe(name, security))
        {
            var launch = new ProcessStartInfo(Self);
            launch.Arguments = "--worker --config " + Quote(config) + " --pipe " + Quote(name)
                + " --parent " + parent + " --session " + session + " --sid " + Quote(sid)
                + " --nonce " + Quote(nonce) + " --require-admin " + (administrator ? "true" : "false");
            launch.WorkingDirectory = BaseDirectory;
            launch.WindowStyle = ProcessWindowStyle.Hidden;
            launch.UseShellExecute = administrator && !IsAdministrator();
            if (launch.UseShellExecute) launch.Verb = "runas";
            else launch.CreateNoWindow = true;
            using (Process worker = Process.Start(launch))
            {
                if (worker == null) throw new InvalidOperationException("연결 프로세스를 시작하지 못했습니다.");
                var deadline = DateTime.UtcNow.AddMilliseconds(ConnectMilliseconds);
                while (true)
                {
                    IAsyncResult connecting = pipe.BeginWaitForConnection(null, null);
                    try
                    {
                        while (!connecting.AsyncWaitHandle.WaitOne(100))
                        {
                            if (worker.HasExited) throw new InvalidOperationException("관리자 연결 프로세스가 시작 전에 종료되었습니다.");
                            if (DateTime.UtcNow >= deadline) throw new TimeoutException();
                        }
                        pipe.EndWaitForConnection(connecting);
                    }
                    finally { connecting.AsyncWaitHandle.Close(); }
                    uint peer;
                    if (GetNamedPipeClientProcessId(pipe.SafePipeHandle.DangerousGetHandle(), out peer) && peer == (uint)worker.Id)
                        break;
                    pipe.Disconnect(); // Reject another connector; never forward its bytes.
                }
                var frames = new Frames(pipe);
                byte kind; byte[] hello = frames.ReadWithDeadline(out kind, 10000);
                string expected = nonce + "|" + sid + "|" + session + "|";
                string actual = Utf8.GetString(hello);
                if (kind != 4 || !actual.StartsWith(expected, StringComparison.Ordinal)
                    || (actual != expected + "true" && actual != expected + "false")
                    || (administrator && actual != expected + "true"))
                    throw new InvalidOperationException("같은 사용자·세션의 관리자 연결을 확인하지 못했습니다.");
                frames.Write(4, Utf8.GetBytes(nonce));
                Stream input = Console.OpenStandardInput(), output = Console.OpenStandardOutput(), errors = Console.OpenStandardError();
                var sender = new Thread(delegate()
                {
                    try
                    {
                        byte[] buffer = new byte[MaxFrame]; int count;
                        while ((count = input.Read(buffer, 0, buffer.Length)) > 0) frames.Write(1, Slice(buffer, count));
                        frames.Write(2, new byte[0]);
                    }
                    catch { try { pipe.Dispose(); } catch { } }
                });
                sender.IsBackground = true; sender.Start();
                while (true)
                {
                    byte[] data = frames.Read(out kind);
                    if (kind == 1) WriteConsole(output, data);
                    else if (kind == 2) WriteConsole(errors, data);
                    else if (kind == 3 && data.Length == 4) return BitConverter.ToInt32(data, 0);
                    else throw new InvalidDataException("알 수 없는 내부 연결 응답입니다.");
                }
            }
        }
    }

    static NamedPipeServerStream LocalPipe(string name, PipeSecurity security)
    {
        byte[] descriptor=security.GetSecurityDescriptorBinaryForm();
        var pinned=GCHandle.Alloc(descriptor,GCHandleType.Pinned);
        IntPtr handle;
        try
        {
            var attributes=new SecurityAttributes(); attributes.Length=Marshal.SizeOf(typeof(SecurityAttributes));
            attributes.Descriptor=pinned.AddrOfPinnedObject(); attributes.Inherit=0;
            // First instance + reject all remote clients, in addition to SID/PID authentication.
            handle=CreateNamedPipe(@"\\.\pipe\"+name,0x40080003,0x8,1,MaxFrame,MaxFrame,10000,ref attributes);
            if(handle==new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally { pinned.Free(); }
        var owned=new SafePipeHandle(handle,true);
        try { return new NamedPipeServerStream(PipeDirection.InOut,true,false,owned); }
        catch { owned.Dispose(); throw; }
    }

    static int Worker(Dictionary<string,string> options, string config)
    {
        if (options.ContainsKey("normal")) throw new ArgumentException("내부 연결의 실행 방식을 바꿀 수 없습니다.");
        string pipeName = Require(options, "pipe"), nonce = Require(options, "nonce"), sid = Require(options, "sid");
        uint parent = UInt32.Parse(Require(options, "parent")), session = UInt32.Parse(Require(options, "session"));
        string required = Require(options, "require-admin");
        if (required != "true" && required != "false") throw new ArgumentException("알 수 없는 권한 방식입니다.");
        if (!System.Text.RegularExpressions.Regex.IsMatch(pipeName, @"\ACompanyComputerUse\.[a-f0-9]{32}\z")
            || !System.Text.RegularExpressions.Regex.IsMatch(nonce, @"\A[a-f0-9]{64}\z")
            || CurrentSid() != sid || Session((uint)Process.GetCurrentProcess().Id) != session
            || !SameProcess(parent) || (required == "true" && !IsAdministrator()))
            throw new InvalidOperationException("다른 사용자·세션 또는 부족한 권한의 연결은 실행하지 않습니다.");
        using (var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous))
        {
            pipe.Connect(10000);
            uint peer;
            if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle.DangerousGetHandle(), out peer) || peer != parent)
                throw new InvalidOperationException("원래 MCP 연결 프로세스가 아닙니다.");
            var frames = new Frames(pipe);
            frames.Write(4, Utf8.GetBytes(nonce + "|" + sid + "|" + session + "|" + (IsAdministrator() ? "true" : "false")));
            byte kind; byte[] accepted = frames.ReadWithDeadline(out kind, 10000);
            if (kind != 4 || Utf8.GetString(accepted) != nonce) throw new InvalidOperationException("연결 인증을 완료하지 못했습니다.");
            var start = ServerStart(config);
            using (var child = new Process())
            {
                child.StartInfo = start;
                if (!child.Start()) throw new InvalidOperationException("MCP를 시작하지 못했습니다.");
                var disconnected = new ManualResetEvent(false);
                var incoming = new Thread(delegate()
                {
                    try
                    {
                        while (true)
                        {
                            byte type; byte[] data = frames.Read(out type);
                            if (type == 2 && data.Length == 0) break;
                            if (type != 1) throw new InvalidDataException();
                            child.StandardInput.BaseStream.Write(data, 0, data.Length);
                            child.StandardInput.BaseStream.Flush();
                        }
                    }
                    catch { }
                    finally { try { child.StandardInput.Close(); } catch { } disconnected.Set(); }
                });
                incoming.IsBackground = true; incoming.Start();
                Thread stdout = Pump(child.StandardOutput.BaseStream, frames, 1, disconnected);
                Thread stderr = Pump(child.StandardError.BaseStream, frames, 2, disconnected);
                while (!child.WaitForExit(100))
                {
                    if (disconnected.WaitOne(0))
                    {
                        // Only our exact MCP process; never terminate application processes.
                        if (!child.WaitForExit(15000)) { child.Kill(); child.WaitForExit(5000); }
                        break;
                    }
                }
                stdout.Join(5000); stderr.Join(5000);
                int code = child.HasExited ? child.ExitCode : 1;
                try { frames.Write(3, BitConverter.GetBytes(code)); } catch { }
                return code;
            }
        }
    }

    static ProcessStartInfo ServerStart(string config)
    {
        string runtime = Path.Combine(BaseDirectory, "runtime"), python = Path.Combine(runtime, "python.exe");
        string server = Path.Combine(BaseDirectory, "server.py");
        if (!File.Exists(python) || !File.Exists(server)) throw new FileNotFoundException("ZIP 전체를 압축 해제해 주세요. MCP 실행 파일이 없습니다.");
        var start = new ProcessStartInfo(python, "-B -s " + Quote(server) + " --config " + Quote(config));
        start.WorkingDirectory = BaseDirectory;
        start.UseShellExecute = false; start.CreateNoWindow = true;
        start.RedirectStandardInput = true; start.RedirectStandardOutput = true; start.RedirectStandardError = true;
        var remove = new List<string>();
        foreach (string key in start.EnvironmentVariables.Keys)
            if (key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase) || key == "TCL_LIBRARY" || key == "TK_LIBRARY") remove.Add(key);
        foreach (string key in remove) start.EnvironmentVariables.Remove(key);
        start.EnvironmentVariables["PYTHONHOME"] = runtime;
        start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        start.EnvironmentVariables["TCL_LIBRARY"] = Path.Combine(runtime, "tcl", "tcl8.6");
        start.EnvironmentVariables["TK_LIBRARY"] = Path.Combine(runtime, "tcl", "tk8.6");
        return start;
    }

    static Thread Pump(Stream stream, Frames frames, byte type, ManualResetEvent disconnected)
    {
        var thread = new Thread(delegate()
        {
            try { byte[] buffer = new byte[MaxFrame]; int count; while ((count=stream.Read(buffer,0,buffer.Length))>0) frames.Write(type,Slice(buffer,count)); }
            catch { disconnected.Set(); }
        });
        thread.IsBackground = true; thread.Start(); return thread;
    }
    static byte[] Slice(byte[] data, int count) { byte[] copy=new byte[count]; Buffer.BlockCopy(data,0,copy,0,count); return copy; }
    static void WriteConsole(Stream stream, byte[] data) { stream.Write(data,0,data.Length); stream.Flush(); }
    static void Error(string text) { try { WriteConsole(Console.OpenStandardError(), Utf8.GetBytes("computer-use-admin: " + text + "\n")); } catch { } }
    internal static string Quote(string text)
    {
        var result = new StringBuilder("\""); int slashes=0;
        foreach(char value in text)
        {
            if (value=='\\') { slashes++; continue; }
            if (value=='"') { result.Append('\\',slashes*2+1).Append('"'); slashes=0; continue; }
            result.Append('\\',slashes).Append(value); slashes=0;
        }
        result.Append('\\',slashes*2).Append('"'); return result.ToString();
    }
    sealed class Frames
    {
        readonly Stream stream; readonly object gate=new object();
        internal Frames(Stream value) { stream=value; }
        internal void Write(byte type, byte[] data)
        {
            if (data.Length>MaxFrame) throw new InvalidDataException("내부 연결 조각이 너무 큽니다.");
            lock(gate) { stream.WriteByte(type); byte[] length=BitConverter.GetBytes(data.Length); stream.Write(length,0,4); stream.Write(data,0,data.Length); stream.Flush(); }
        }
        internal byte[] Read(out byte type)
        {
            int value=stream.ReadByte(); if(value<0) throw new EndOfStreamException(); type=(byte)value;
            byte[] length=Exact(4); int count=BitConverter.ToInt32(length,0);
            if(count<0 || count>MaxFrame) throw new InvalidDataException("잘못된 내부 연결 조각입니다.");
            return Exact(count);
        }
        byte[] Exact(int count)
        {
            byte[] data=new byte[count]; int offset=0;
            while(offset<count) { int read=stream.Read(data,offset,count-offset); if(read<=0) throw new EndOfStreamException(); offset+=read; }
            return data;
        }
        internal byte[] ReadWithDeadline(out byte type, int milliseconds)
        {
            byte received=0; byte[] result=null; Exception failure=null; var finished=new ManualResetEvent(false);
            var reader=new Thread(delegate(){try{result=Read(out received);}catch(Exception error){failure=error;}finally{finished.Set();}});
            reader.IsBackground=true; reader.Start();
            if(!finished.WaitOne(milliseconds)) { stream.Dispose(); throw new TimeoutException(); }
            if(failure!=null) throw failure;
            type=received; return result;
        }
    }
}
