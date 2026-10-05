// Build with the Windows .NET Framework compiler. No administrator rights needed.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    private static readonly object LogLock = new object();
    private static string logPath;
    private const string LogHeader = "Computer Use MCP startup log v1";

    [STAThread]
    private static int Main(string[] args)
    {
        bool selfTest = args.Length == 1 && args[0] == "--self-test";
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string runtime = Path.Combine(baseDir, "runtime");
        string python = Path.Combine(runtime, selfTest ? "python.exe" : "pythonw.exe");
        string app = Path.Combine(baseDir, "setup.py");
        try
        {
            if (selfTest)
            {
                // A GUI executable has no console code page. Configure the redirected
                // byte streams directly; Console.OutputEncoding calls SetConsoleOutputCP.
                StreamWriter stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
                StreamWriter stderr = new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false));
                stdout.AutoFlush = true;
                stderr.AutoFlush = true;
                Console.SetOut(stdout);
                Console.SetError(stderr);
            }
            if (!File.Exists(python))
                throw new FileNotFoundException("실행에 필요한 파일이 없습니다. ZIP 전체를 다시 압축 해제해 주세요.", python);
            if (!selfTest && !File.Exists(app))
                throw new FileNotFoundException("설정 화면 파일이 없습니다. ZIP 전체를 다시 압축 해제해 주세요.", app);

            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = python;
            start.WorkingDirectory = baseDir;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
            List<string> pythonKeys = new List<string>();
            foreach (string key in start.EnvironmentVariables.Keys)
                if (key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase)) pythonKeys.Add(key);
            foreach (string key in pythonKeys) start.EnvironmentVariables.Remove(key);
            start.EnvironmentVariables["PYTHONHOME"] = runtime;
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["TCL_LIBRARY"] = Path.Combine(runtime, "tcl", "tcl8.6");
            start.EnvironmentVariables["TK_LIBRARY"] = Path.Combine(runtime, "tcl", "tk8.6");

            if (selfTest)
            {
                string check = "import tkinter,ssl,json,subprocess,sys,os; "
                    + "assert os.path.normcase(os.path.realpath(sys.prefix)) == os.path.normcase(os.path.realpath(os.environ['PYTHONHOME'])); "
                    + "print(json.dumps({'ok':True,'python':sys.version.split()[0],'executable':sys.executable,'prefix':sys.prefix,'tcl':tkinter.Tcl().eval('info patchlevel'),'imports':['tkinter','ssl','json','subprocess']},ensure_ascii=False))";
                start.Arguments = "-B -s -c " + QuoteArgument(check);
            }
            else
            {
                StringBuilder arguments = new StringBuilder("-B -s ");
                arguments.Append(QuoteArgument(app));
                foreach (string argument in args)
                    arguments.Append(" ").Append(QuoteArgument(argument));
                start.Arguments = arguments.ToString();
            }

            StringBuilder output = new StringBuilder();
            StringBuilder errors = new StringBuilder();
            using (Process process = new Process())
            {
                process.StartInfo = start;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    if (selfTest) { lock (output) output.AppendLine(e.Data); }
                    else WriteLog(baseDir, e.Data);
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    lock (errors) errors.AppendLine(e.Data);
                    if (!selfTest) WriteLog(baseDir, e.Data);
                };
                if (!process.Start())
                    throw new InvalidOperationException("설정 창을 실행하지 못했습니다.");
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                if (selfTest && !process.WaitForExit(30000))
                {
                    try { process.Kill(); } catch { }
                    process.WaitForExit();
                    throw new TimeoutException("실행 환경 확인 시간이 초과되었습니다.");
                }
                process.WaitForExit(); // Also waits for asynchronous pipe readers to finish.
                if (selfTest)
                {
                    Console.Out.Write(output.ToString());
                    Console.Error.Write(errors.ToString());
                    if (process.ExitCode != 0)
                        WriteLog(baseDir, "실행 환경 확인 실패\r\n" + errors.ToString());
                }
                else if (process.ExitCode != 0)
                {
                    WriteLog(baseDir, "설정 창 종료 코드: " + process.ExitCode);
                    ShowFailure("설정 창을 실행하는 동안 오류가 발생했습니다.");
                }
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            WriteLog(baseDir, error.ToString());
            if (selfTest) Console.Error.WriteLine(error.ToString());
            else ShowFailure(error.Message);
            return 1;
        }
    }

    // Windows CommandLineToArgvW/CRT convention; preserves quotes and trailing slashes.
    internal static string QuoteArgument(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"')
            {
                result.Append('\\', slashes * 2 + 1).Append('"');
                slashes = 0;
                continue;
            }
            result.Append('\\', slashes).Append(ch);
            slashes = 0;
        }
        result.Append('\\', slashes * 2).Append('"');
        return result.ToString();
    }

    private static void WriteLog(string baseDir, string text)
    {
        lock (LogLock)
        {
            string record = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + text + Environment.NewLine;
            if (logPath != null)
            {
                try { AppendLog(logPath, record); return; }
                catch { }
            }
            string[] folders = new string[] {
                baseDir,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ComputerUseMCP")
            };
            foreach (string folder in folders)
            {
                try
                {
                    Directory.CreateDirectory(folder);
                    string candidate = Path.Combine(folder, "startup-log.txt");
                    AppendLog(candidate, record);
                    logPath = candidate;
                    return;
                }
                catch { }
            }
        }
    }

    private static void AppendLog(string path, string record)
    {
        using (FileStream stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read))
        using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
        {
            if (stream.Length == 0) writer.WriteLine(LogHeader);
            writer.Write(record);
        }
    }

    private static void ShowFailure(string message)
    {
        string detail = logPath == null ? "\r\n\r\nZIP을 쓰기 가능한 폴더에 압축 해제한 뒤 다시 실행해 주세요."
            : "\r\n\r\n확인 기록: " + logPath;
        MessageBox.Show(message + detail, "Computer Use MCP 설정", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
