using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal static class SaltLrcLauncher
{
    [STAThread]
    private static int Main()
    {
        string root = AppContext.BaseDirectory;
        string[] candidates = {
            Path.Combine(root, ".runtime", "python.exe"),
            Path.Combine(root, ".venv", "Scripts", "python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe"),
            "python.exe",
            "py.exe"
        };
        string python = candidates.FirstOrDefault(File.Exists) ?? candidates.Last();
        try
        {
            var info = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-u -m saltlrc serve",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = false
            };
            Process.Start(info);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "SaltLRC Studio 需要 Python 3.10 或更高版本。\n\n" +
                "请先安装 Python，或在项目目录运行 setup-ai.ps1。\n\n" + error.Message,
                "SaltLRC Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
