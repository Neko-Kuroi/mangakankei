# これは Colab テスト環境用の起動ハーネスです（アプリ本体ではありません）。
# 本体の起動は uvicorn main:app を直接使ってください。
# トンネル（bore / cloudflared）はテスト環境の都合であり、本体はトンネル非依存です。
import time
import os
#import hashlib
#import tempfile
import time
import base64
import subprocess
import shutil
import queue
import threading
import sys
import re
from IPython.display import HTML, display
import logging
import requests # Import the requests library

exposed_url = ""
# ログの設定
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ログ読み取り関数は削除またはコメントアウト
def read_process_output(process, stream, prefix="", url_pattern=None, url_queue=None):
    """プロセスの出力をリアルタイムで読み取り、表示する関数。

    url_pattern と url_queue が渡された場合、その流れの唯一の読み手として
    URL の行も探し、見つけたら url_queue へ渡す（本体は流れを直接読まない）。
    """
    while True:
        line = stream.readline()
        if line:
            print(f"{prefix}{line.strip()}")
            if url_pattern and url_queue:
                match = re.search(url_pattern, line)
                if match:
                    url_queue.put(match.group(0).strip())
        elif process.poll() is not None:
            try:
                remaining = stream.read()
                if remaining:
                     print(f"{prefix}{remaining.strip()}")
                     if url_pattern and url_queue:
                         match = re.search(url_pattern, remaining)
                         if match:
                             url_queue.put(match.group(0).strip())
            except ValueError:
                 pass
            break
        else:
            time.sleep(0.01)
    try:
        stream.close()
    except Exception as e:
        logger.debug(f"Error closing stream in read_process_output: {e}")


def setup_bore_tunnel():
    """Rust製のboreトンネルの設定"""
    print("🦀 Bore トンネルをセットアップしています...")

    # boreのダウンロードとインストール
    # -nc オプションは、ファイルが既に存在する場合に再ダウンロードしないようにします。
    os.system('sudo wget -nc https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
    # ダウンロードしたアーカイブを解凍します。
    os.system('sudo tar -zxvf bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
    # 実行権限を付与し
    os.system('sudo chmod 764 bore')

    # FastAPIアプリケーションをバックグラウンドで起動します。
    print("🚀 FastAPI アプリケーションを起動しています...")
    flask_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--reload"],
        # stdout=subprocess.PIPE, # ログ出力しないため削除
        # stderr=subprocess.PIPE, # ログ出力しないため削除
        # text=True,
        # bufsize=1
    )

    # Flaskプロセスの出力を読み取るスレッドは削除
    # flask_stdout_thread = threading.Thread(target=read_process_output, args=(flask_process, flask_process.stdout, "[FLASK_OUT] "))
    # flask_stderr_thread = threading.Thread(target=read_process_output, args=(flask_process, flask_process.stderr, "[FLASK_ERR] "))
    # flask_stdout_thread.daemon = True
    # flask_stderr_thread.daemon = True
    # flask_stdout_thread.start()
    # flask_stderr_thread.start()


    time.sleep(10)  # FastAPIサーバーが完全に起動するまで少し長めに7秒待ちます。

    # boreトンネルの起動
    print("🌐 bore トンネルを開始しています...")
    # boreをローカルポート5000からbore.pubへのトンネルとして起動します。
    # stdoutとstderrをsubprocess.PIPEにリダイレクトし、テキストモードでキャプチャします。
    bore_process = subprocess.Popen(['sudo', './bore', 'local', '8000', '--to', 'bore.pub'],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   text=True,
                                   bufsize=1 # 行バッファリングを無効にしてリアルタイム出力を試みる
                                   )

    url_queue = queue.Queue()
    bore_stdout_thread = threading.Thread(target=read_process_output, args=(bore_process, bore_process.stdout, "[BORE_OUT] "),
                                          kwargs={"url_pattern": r'(bore\.pub:\d+)', "url_queue": url_queue})
    bore_stderr_thread = threading.Thread(target=read_process_output, args=(bore_process, bore_process.stderr, "[BORE_ERR] "))
    bore_stdout_thread.daemon = True
    bore_stderr_thread.daemon = True
    bore_stdout_thread.start()
    bore_stderr_thread.start()


    print("🔍 トンネルURLを待機しています...")
    url_found = False
    url = ""
    # タイムアウトを設定し、無限ループにならないようにします
    start_time = time.time()
    timeout = 130 # タイムアウト

    # URLは読み取りスレッドがキューで渡す（stdoutの読み手はスレッドだけにする）
    while time.time() - start_time < timeout:
        try:
            url = url_queue.get(timeout=0.5)
            url_found = True
            break
        except queue.Empty:
            pass

        # boreが終了したか確認（エラーになった場合など）
        if bore_process.poll() is not None:
            print("Boreプロセスが予期せず終了しました。")
            break

    if url_found:
        print(f"✅ トンネルが開始されました: {url}")
        # IPython.display.HTML を使用して、クリック可能なURLを生成し表示します。
        display(HTML(f'<a href="http://{url}" target="_blank" style="font-size:18px; color:pink;">{url}</a>'))
        # boreトンネルへのcurlテスト
        print("\n--- Boreトンネルへの内部curlテスト ---")
        try:
            # 外部からアクセスするURLに対してcurlを実行
            curl_url = f"http://{url}"
            curl_result = subprocess.run(['curl', '-s', '-I', curl_url], capture_output=True, text=True, timeout=10)
            print("curl出力（ヘッダのみ）：")
            print(curl_result.stdout.strip())
            if "200 OK" in curl_result.stdout:
                print("✅ curlテスト成功: HTTP 200 OK を受信しました。ブラウザでアクセスできるはずです。")
            else:
                print("❌ curlテスト失敗: 予期しない応答コードを受信しました。")
                # print(f"Boreの応答全文:\n{curl_result.stdout}\n{curl_result.stderr}") # スレッドが出力済みのため不要
        except subprocess.TimeoutExpired:
            print("❌ curlテスト失敗: タイムアウトしました。Boreサービスが応答していません。")
        except Exception as e:
            print(f"❌ curlテスト中にエラーが発生しました: {e}")
        print("----------------------------")

    else:
        print("⚠️ トンネルURLの取得に失敗しました。")
        # Boreプロセスの標準出力と標準エラー出力の表示は削除
        print("Boreプロセスの標準出力と標準エラー出力を確認してください。")
        # 出力は読み取りスレッドがすべて表示済みなので、ここでは読まない
        bore_stdout_thread.join(timeout=5)
        bore_stderr_thread.join(timeout=5)


    return flask_process, bore_process, url

def setup_cloudflare_tunnel():
    """Cloudflare Tunnelの設定"""
    print("☁️ Cloudflare Tunnel をセットアップしています...")

    # cloudflaredのダウンロードとインストール
    os.system('sudo wget -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb')
    os.system('sudo dpkg -i cloudflared-linux-amd64.deb 2>/dev/null')

    # 実行ファイルの場所は導入方法で変わる（deb は /usr/bin、生バイナリは
    # /usr/local/bin 等）ので、パスを直書きせず PATH から解決する。
    # 見つからなければ、インストール失敗として明確なエラーで止める。
    cloudflared_path = shutil.which('cloudflared')
    if not cloudflared_path:
        print("❌ cloudflared が見つかりません。インストールに失敗した可能性があります。")
        return None, None, ""

    # FastAPIアプリケーションをバックグラウンドで起動
    print("🚀 FastAPI アプリケーションを起動しています...")
    flask_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"],
        stdout=subprocess.PIPE, # stdoutとstderrをキャプチャしてスレッドで読み取る
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    # Flaskプロセスの出力を読み取るスレッド
    flask_stdout_thread = threading.Thread(target=read_process_output, args=(flask_process, flask_process.stdout, "[FLASK_OUT] "))
    flask_stderr_thread = threading.Thread(target=read_process_output, args=(flask_process, flask_process.stderr, "[FLASK_ERR] "))
    flask_stdout_thread.daemon = True
    flask_stderr_thread.daemon = True
    flask_stdout_thread.start()
    flask_stderr_thread.start()

    # サーバーの起動を待つ
    if not wait_for_flask_server(port=8000, timeout=60):
        print("❌ FastAPIアプリケーションが指定時間内に起動しませんでした。")
        flask_process.terminate()
        flask_stdout_thread.join(timeout=5)
        flask_stderr_thread.join(timeout=5)
        return None, None, ""

    # Cloudflareトンネルの起動
    print("🌐 Cloudflare トンネルを開始しています...")
    tunnel_process = subprocess.Popen([cloudflared_path, 'tunnel', '--url', 'http://localhost:8000'],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     text=True,
                                     bufsize=1)

    # Cloudflare Tunnelプロセスの出力を読み取るスレッドを再度有効化
    url_queue = queue.Queue()
    tunnel_stdout_thread = threading.Thread(target=read_process_output, args=(tunnel_process, tunnel_process.stdout, "[CF_OUT] "))
    tunnel_stderr_thread = threading.Thread(target=read_process_output, args=(tunnel_process, tunnel_process.stderr, "[CF_ERR] "),
                                            kwargs={"url_pattern": r'(https:\/\/[^\s]+\.trycloudflare\.com)', "url_queue": url_queue})
    tunnel_stdout_thread.daemon = True
    tunnel_stderr_thread.daemon = True
    tunnel_stdout_thread.start()
    tunnel_stderr_thread.start()


    # URLは読み取りスレッドがキューで渡す（stderrの読み手はスレッドだけにする）
    url_found = False
    url = ""
    start_time = time.time()
    timeout = 130

    while time.time() - start_time < timeout:
        try:
            url = url_queue.get(timeout=0.5)
            url_found = True
            break
        except queue.Empty:
            pass

        if tunnel_process.poll() is not None:
            print("Cloudflare Tunnelプロセスが予期せず終了しました。")
            break

    if url_found:
        print(f"✅ Cloudflare トンネルが開始されました: {url}")
        display(HTML(f'<a href="{url}" target="_blank" style="font-size:18px; color:pink;">app exposed url: {url}</a>'))
    else:
        print("⚠️ CloudflareトンネルURLの取得に失敗しました。")
        # Cloudflare Tunnelプロセスの標準出力と標準エラー出力の表示は削除
        print("Cloudflare Tunnelプロセスの標準出力と標準エラー出力を確認してください。")
        # 出力は読み取りスレッドがすべて表示済みなので、ここでは読まない
        tunnel_stdout_thread.join(timeout=5)
        tunnel_stderr_thread.join(timeout=5)


    return flask_process, tunnel_process, url

def wait_for_flask_server(port=8000, timeout=60):
    """flaskサーバーが起動し、リクエストに応答するのを待機します。"""
    url = f"http://localhost:{port}"
    print(f"Waiting for Flask server to start at {url}...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                print(f"✅ Flask server is up and running! Status code: {response.status_code}")
                return True
            else:
                print(f"[Health Check] Received status code {response.status_code} for {url}. Waiting...")
        except requests.exceptions.ConnectionError:
            pass # サーバーがまだ起動していない、または接続を拒否している
        except Exception as e:
            print(f"Error during Flask server health check: {e}")
            pass
        time.sleep(1)
    print(f"❌ Flask server did not respond within {timeout} seconds.")
    return False


def get_colab_external_ip():
    """Colabの外部IPアドレスを取得します。"""
    try:
        result = subprocess.run(['curl', 'ipinfo.io/ip'], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except Exception as e:
        print(f"⚠️ 外部IPアドレスの取得に失敗しました: {e}")
        return "UNKNOWN_IP"

if __name__ == "__main__":
    selected_tunnel_service = ""
    while True:
        print("\n--- トンネル方法を選択してください ---")
        print("1. Bore")
        print("2. Cloudflared")
        #print("3. Localtunnel (Node.js/npm が必要です)")
        choice = input("選択 (1/2): ").strip()

        if choice == '1':
            selected_tunnel_service = "bore"
            break
        elif choice == '2':
            selected_tunnel_service = "cloudflared"
            break
        else:
            print("無効な選択です。1 または 2 を入力してください。")


    if selected_tunnel_service == "bore":
        flask_process, bore_process, exposed_url = setup_bore_tunnel()
        # bore_process.wait() # waitは削除
    elif selected_tunnel_service == "cloudflared":
        flask_process, cloudflared_process, exposed_url = setup_cloudflare_tunnel()
        #tunnel_process.wait() # waitは削除

    # プロセスがバックグラウンドで実行され続けるように、メインスレッドは特に待機しない
    # Colabのセルが実行中である限り、子プロセスは実行を続けます。
    # セルの実行が完了すると、これらのプロセスも通常終了します。

    print(f"\nColab 外部IPアドレス: {get_colab_external_ip()}")
    print(f"expose : {exposed_url}")
