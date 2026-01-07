import os
import time
import dotenv
from binance.um_futures import UMFutures

# 1. 加载你的 2.env 配置
env_file = "2.env"
if not os.path.exists(env_file):
    print(f"❌ 错误：找不到 {env_file} 文件，请检查文件名是否正确。")
    exit()

dotenv.load_dotenv(env_file)

def run_diagnostic():
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")
    proxy = os.getenv("HTTPS_PROXY")
    
    print(f"--- 正在开始全流程联通测试 ---")
    print(f"使用配置文件: {env_file}")
    print(f"使用代理地址: {proxy}")
    
    proxies = {'https': proxy, 'http': proxy} if proxy else None
    
    try:
        # 初始化客户端 (注意：UMFutures 初始化不接受 recvWindow)
        client = UMFutures(
            key=api_key, 
            secret=api_secret, 
            proxies=proxies
        )
        
        # 步骤 1: 测试代理与基础连接
        print("\n[步骤 1] 测试网络连接...")
        server_time_resp = client.time()
        server_time = server_time_resp['serverTime']
        local_time = int(time.time() * 1000)
        diff = local_time - server_time
        
        print(f"✅ 网络正常！")
        print(f"   服务器时间: {server_time}")
        print(f"   本地系统时间: {local_time}")
        print(f"   时间偏差: {diff}ms (正值代表本地快，负值代表本地慢)")
        
        # 步骤 2: 测试 API Key 与账户权限 (在这里传入 recvWindow)
        print("\n[步骤 2] 测试 API 权限与余额...")
        # 将 recvWindow 放在具体的请求函数中
        account = client.account(recvWindow=10000)
        
        # 检查是否能读取到余额
        balance = float(account.get('totalWalletBalance', 0))
        print(f"✅ API 验证通过！")
        print(f"💰 账户当前余额: {balance} USDT")
        
        # 步骤 3: 检查合约交易权限
        print("\n[步骤 3] 检查合约交易权限...")
        client.get_position_risk(symbol=os.getenv("SYMBOL", "ETHUSDC"), recvWindow=10000)
        print(f"✅ 合约交易权限已开启！")
        
        print("\n" + "="*30)
        print("🎉 恭喜！整体流程已全部打通。")
        print(f"你可以放心地运行: python runbot_hedge.py")
        print("="*30)

    except Exception as e:
        print(f"\n❌ 流程中断，错误详情:")
        error_msg = str(e)
        if "-1021" in error_msg:
            print("   原因：时间同步错误 (Timestamp ahead of server)。")
            print("   解决建议：")
            print("   1. 请在 Windows 设置中点击“立即同步”系统时间。")
            print("   2. 脚本已将 recvWindow 应用到请求中，请重新运行。")
        elif "API-key format invalid" in error_msg:
            print("   原因：API_KEY 格式不正确。")
        elif "Signature for this request is not valid" in error_msg:
            print("   原因：API_SECRET 错误或签名逻辑受时间偏差影响。")
        else:
            print(f"   具体报错内容: {error_msg}")

if __name__ == "__main__":
    run_diagnostic()