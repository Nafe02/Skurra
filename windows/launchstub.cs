using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

static class Program {
    // This is the file Windows users download. It is not the Python app.
    // The Python app is a folder zipped onto the end of this file. Putting
    // python312.dll beside Skurra.exe, in a normal folder, is what stops
    // "Failed to load Python DLL" from the temporary _MEI folder.
    static int Main() {
        try {
            string self = Assembly.GetExecutingAssembly().Location;
            if (string.IsNullOrEmpty(self))
                self = Process.GetCurrentProcess().MainModule.FileName;
            byte[] file = File.ReadAllBytes(self);
            if (file.Length < 16) throw new InvalidDataException("Skurra.exe is incomplete.");
            long zipAt = BitConverter.ToInt64(file, file.Length - 8);
            // The compiler that ships with .NET Framework cannot index a byte
            // array with a long, so the range is checked and then narrowed.
            if (zipAt < 0 || zipAt > file.Length - 10)
                throw new InvalidDataException("Skurra.exe does not contain the app.");
            int zipIndex = (int)zipAt;
            if (file[zipIndex] != (byte)'P' || file[zipIndex + 1] != (byte)'K')
                throw new InvalidDataException("Skurra.exe does not contain the app.");

            string tempZip = Path.Combine(Path.GetTempPath(), "skurra-payload.zip");
            using (var src = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) {
                src.Seek(zipAt, SeekOrigin.Begin);
                using (var dst = File.Create(tempZip)) {
                    long left = file.Length - 8 - zipAt;
                    byte[] buf = new byte[1 << 16];
                    while (left > 0) {
                        int n = src.Read(buf, 0, (int)Math.Min(buf.Length, left));
                        if (n <= 0) break;
                        dst.Write(buf, 0, n);
                        left -= n;
                    }
                }
            }

            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Skurra");
            string stage = Path.Combine(root, "app-new");
            string dest = Path.Combine(root, "app");
            Directory.CreateDirectory(root);
            if (Directory.Exists(stage)) Directory.Delete(stage, true);
            ZipFile.ExtractToDirectory(tempZip, stage);
            string built = Path.Combine(stage, "Skurra");
            string exe = Path.Combine(built, "Skurra.exe");
            string dll = Path.Combine(built, "python312.dll");
            if (!File.Exists(dll)) dll = Path.Combine(built, "_internal", "python312.dll");
            if (!File.Exists(exe) || !File.Exists(dll))
                throw new FileNotFoundException("The Windows app is missing python312.dll.");
            if (Directory.Exists(dest)) Directory.Delete(dest, true);
            Directory.Move(built, dest);
            try { Directory.Delete(stage, true); } catch { }
            try { File.Delete(tempZip); } catch { }

            Process.Start(new ProcessStartInfo(Path.Combine(dest, "Skurra.exe")) { UseShellExecute = true });
            string started = Path.Combine(root, "started.json");
            for (int i = 0; i < 45 && !File.Exists(started); i++) Thread.Sleep(1000);
            return 0;
        } catch (Exception ex) {
            try {
                string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Skurra");
                Directory.CreateDirectory(root);
                File.WriteAllText(Path.Combine(root, "launch-error.txt"), ex.ToString());
            } catch { }
            MessageBox.Show(ex.Message, "Skurra");
            return 1;
        }
    }
}
