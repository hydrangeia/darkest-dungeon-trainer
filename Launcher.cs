using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    private const string WindowTitle = "暗黑地牢测试修改器";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            string runtimeDir = Path.Combine(baseDir, "runtime");
            string appDir = Path.Combine(baseDir, "app");
            string appScript = Path.Combine(appDir, "trainer_ui.py");
            string pythonw = Path.Combine(runtimeDir, "pythonw.exe");
            string python = Path.Combine(runtimeDir, "python.exe");
            string tclDir = Path.Combine(runtimeDir, "tcl", "tcl8.6");
            string tkDir = Path.Combine(runtimeDir, "tcl", "tk8.6");

            if (!Directory.Exists(runtimeDir) || !File.Exists(Path.Combine(runtimeDir, "python313.dll")))
                return ShowError("便携运行环境不完整。请从完整发行包启动，并确认 runtime 文件夹存在。", 2);
            if (!File.Exists(appScript))
                return ShowError("没有找到界面文件：\n" + appScript + "\n请重新解压完整发行包。", 2);
            if (!Directory.Exists(tclDir) || !Directory.Exists(tkDir))
                return ShowError("Tk 图形界面运行库不完整。请重新解压完整发行包。", 2);

            Environment.SetEnvironmentVariable("TCL_LIBRARY", tclDir);
            Environment.SetEnvironmentVariable("TK_LIBRARY", tkDir);

            bool verifyMode = IsVerificationRequest(args);
            string executable = verifyMode ? python : pythonw;
            if (!File.Exists(executable))
                return ShowError("没有找到随包提供的 Python 运行程序：\n" + executable, 2);

            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = executable;
            startInfo.Arguments = "-B -X utf8 -I " + QuoteArgument(appScript) + JoinArguments(args);
            startInfo.WorkingDirectory = appDir;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = verifyMode;

            if (verifyMode)
                return RunVerification(startInfo);

            using (Process process = Process.Start(startInfo))
            {
                if (process == null)
                    return ShowError("Python 界面进程没有启动。", 2);
                process.WaitForExit();
                if (process.ExitCode != 0)
                    return ShowError("修改器界面异常退出。退出代码：" + process.ExitCode + "\n请重新解压完整发行包，或联系维护者检查运行环境。", process.ExitCode);
            }
            return 0;
        }
        catch (Exception ex)
        {
            return ShowError("启动修改器时发生错误：\n" + ex.Message, 1);
        }
    }

    private static bool IsVerificationRequest(string[] args)
    {
        foreach (string arg in args)
        {
            if (String.Equals(arg, "--self-test", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(arg, "--smoke", StringComparison.OrdinalIgnoreCase))
                return true;
        }
        return false;
    }

    private static int RunVerification(ProcessStartInfo startInfo)
    {
        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;
        startInfo.StandardOutputEncoding = Encoding.UTF8;
        startInfo.StandardErrorEncoding = Encoding.UTF8;

        StringBuilder output = new StringBuilder();
        StringBuilder errors = new StringBuilder();
        using (Process process = new Process())
        {
            process.StartInfo = startInfo;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) output.AppendLine(e.Data);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) errors.AppendLine(e.Data);
            };

            if (!process.Start())
                return ShowError("自检进程没有启动。", 2);
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            process.WaitForExit();

            string details = output.ToString().Trim();
            string errorText = errors.ToString().Trim();
            if (!String.IsNullOrEmpty(errorText))
                details = String.IsNullOrEmpty(details) ? errorText : details + Environment.NewLine + Environment.NewLine + errorText;

            if (process.ExitCode == 0)
            {
                string message = "自检已完成。\n未启动或连接游戏，也未读取或修改真实存档。";
                if (!String.IsNullOrEmpty(details)) message += "\n\n" + details;
                MessageBox.Show(message, WindowTitle, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }

            string failure = "自检失败，退出代码：" + process.ExitCode;
            if (!String.IsNullOrEmpty(details)) failure += "\n\n" + details;
            return ShowError(failure, process.ExitCode);
        }
    }

    private static string JoinArguments(string[] args)
    {
        StringBuilder result = new StringBuilder();
        foreach (string arg in args)
        {
            result.Append(' ');
            result.Append(QuoteArgument(arg));
        }
        return result.ToString();
    }

    private static string QuoteArgument(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return value;

        StringBuilder quoted = new StringBuilder();
        quoted.Append('"');
        int backslashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\')
            {
                backslashes++;
                continue;
            }
            if (ch == '"')
            {
                quoted.Append('\\', backslashes * 2 + 1);
                quoted.Append('"');
                backslashes = 0;
                continue;
            }
            quoted.Append('\\', backslashes);
            backslashes = 0;
            quoted.Append(ch);
        }
        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }

    private static int ShowError(string message, int exitCode)
    {
        MessageBox.Show(message, WindowTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
        return exitCode;
    }
}
