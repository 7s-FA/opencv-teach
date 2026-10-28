using System.Windows;
using System.IO;
using System.Text.Json;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Input;
using So101.Console.Core;
namespace So101.Console.Wpf;
public partial class MainWindow : Window
{
    private readonly MainViewModel viewModel;
    public MainWindow()
    {
        InitializeComponent();
        var args = Environment.GetCommandLineArgs();
        var profileIndex = Array.IndexOf(args, "--live");
        LiveGateway? live = null;
        if (profileIndex >= 0)
        {
            {
                if (profileIndex + 1 >= args.Length) throw new ArgumentException("--live 뒤에 연결 설정 파일을 지정하세요.");
                var profile = JsonSerializer.Deserialize<BridgeProfile>(File.ReadAllText(args[profileIndex + 1]), Protocol.Json)
                    ?? throw new InvalidDataException("연결 설정 파일 오류");
                var profileId = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(profile.Url + profile.CertificateSha256)))[..16];
                var pending = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SO101Console", profileId, "pending");
                live = new(profile, action => Dispatcher.InvokeAsync(action).Task, pending);
                viewModel = new(live);
            }

        }
        else
        {
            var demo = new DemoGateway();
            viewModel = new(demo, demo);
        }
        DataContext = viewModel;
        viewModel.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(MainViewModel.HelpVisible))
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    if (viewModel.HelpVisible) HelpCloseButton.Focus(); else CommandInput.Focus();
                }));
        };
        viewModel.CloseRequested += Close;
        Loaded += async (_, _) => { CommandInput.Focus(); if (live is not null) await live.StartAsync(); };
        PreviewKeyDown += (_, e) =>
        {
            if (e.Key == Key.F1) { viewModel.HelpVisible = true; e.Handled = true; }
            if (e.Key == Key.Escape) { viewModel.HelpVisible = false; CommandInput.Focus(); e.Handled = true; }
        };
        Closed += (_, _) => { viewModel.CloseRequested -= Close; viewModel.Dispose(); };
    }
}
