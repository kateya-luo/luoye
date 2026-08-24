package chat.clearmeeting.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebResourceRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

import java.io.OutputStream;
import java.net.URI;

public class MainActivity extends Activity {
    private static final int AUDIO_PERMISSION = 1001;
    private WebView webView;
    private String serverUrl;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_main);
        webView = findViewById(R.id.webview);
        configureWebView();
        serverUrl = getPreferences(MODE_PRIVATE).getString("server_url", "");
        if (serverUrl.isEmpty()) showServerDialog(); else openServer();
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        webView.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleNavigation(request.getUrl());
            }

            @SuppressWarnings("deprecation")
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleNavigation(Uri.parse(url));
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> {
                    if (isConfiguredOrigin(request.getOrigin()) && hasAudioPermission()) request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    else { request.deny(); requestAudioPermission(); }
                });
            }
        });
        webView.addJavascriptInterface(new DownloadBridge(), "ClearMeetingAndroid");
    }

    private boolean isConfiguredOrigin(Uri origin) {
        try {
            URI configured = URI.create(serverUrl);
            return configured.getScheme().equalsIgnoreCase(origin.getScheme())
                && configured.getHost().equalsIgnoreCase(origin.getHost())
                && effectivePort(configured.getScheme(), configured.getPort()) == effectivePort(origin.getScheme(), origin.getPort());
        }
        catch (Exception ignored) { return false; }
    }

    private int effectivePort(String scheme, int port) {
        if (port >= 0) return port;
        return "https".equalsIgnoreCase(scheme) ? 443 : 80;
    }

    private boolean handleNavigation(Uri target) {
        if (isConfiguredOrigin(target)) return false;
        try { startActivity(new Intent(Intent.ACTION_VIEW, target)); }
        catch (Exception ignored) { Toast.makeText(this, "无法打开外部链接", Toast.LENGTH_SHORT).show(); }
        return true;
    }

    private boolean hasAudioPermission() { return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED; }
    private void requestAudioPermission() { requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, AUDIO_PERMISSION); }

    private void showServerDialog() {
        EditText input = new EditText(this);
        input.setHint("http://100.x.x.x");
        input.setSingleLine(true);
        new AlertDialog.Builder(this).setTitle("连接 Clear Meeting 服务器")
            .setMessage("推荐填写服务器的 Tailscale 私网地址")
            .setView(input).setCancelable(false)
            .setPositiveButton("保存", (dialog, which) -> {
                try {
                    String raw = input.getText().toString().trim();
                    if (!raw.matches("^[a-zA-Z]+://.*")) raw = "http://" + raw;
                    URI uri = URI.create(raw);
                    if (uri.getHost() == null || !("http".equalsIgnoreCase(uri.getScheme())
                            || "https".equalsIgnoreCase(uri.getScheme()))) throw new IllegalArgumentException();
                    serverUrl = uri.getScheme() + "://" + uri.getAuthority();
                    getPreferences(MODE_PRIVATE).edit().putString("server_url", serverUrl).apply();
                    openServer();
                } catch (Exception error) { Toast.makeText(this, "服务器地址无效，请重启应用后重试", Toast.LENGTH_LONG).show(); }
            }).show();
    }

    private void openServer() { if (!hasAudioPermission()) requestAudioPermission(); webView.loadUrl(serverUrl); }

    @Override public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }

    public class DownloadBridge {
        @JavascriptInterface public void saveFile(String filename, String mimeType, String base64) {
            runOnUiThread(() -> {
                try {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, filename);
                    values.put(MediaStore.Downloads.MIME_TYPE, mimeType);
                    values.put(MediaStore.Downloads.IS_PENDING, 1);
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    try (OutputStream stream = getContentResolver().openOutputStream(uri)) { stream.write(Base64.decode(base64, Base64.DEFAULT)); }
                    values.clear(); values.put(MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(uri, values, null, null);
                    Toast.makeText(MainActivity.this, "已保存到下载目录", Toast.LENGTH_SHORT).show();
                } catch (Exception error) { Toast.makeText(MainActivity.this, "保存失败", Toast.LENGTH_LONG).show(); }
            });
        }
    }
}
