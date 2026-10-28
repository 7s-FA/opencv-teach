using System.IO;
using System.Text.Json;
using System.Windows;
namespace So101.Console.Wpf;
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        try { new MainWindow().Show(); }
        catch (Exception exc) when (exc is IOException or ArgumentException or JsonException or UnauthorizedAccessException)
        {
            MessageBox.Show("관제 연결 설정을 읽지 못했습니다.\n" + exc.Message,
                "SO-101 연결 설정", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(2);
        }
    }
}
