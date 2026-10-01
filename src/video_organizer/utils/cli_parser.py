import argparse
from typing import Optional


def get_cli_parser():
    """
    获取命令行解析器实例

    Returns:
        argparse.ArgumentParser实例
    """
    parser = argparse.ArgumentParser(
        description="视频文件自动整理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 配置文件参数
    parser.add_argument("-c", "--config", type=str, help="配置文件路径")

    # 监控目录参数
    parser.add_argument("-m", "--monitor-dir", type=str, help="要监控的目录路径")

    # 显示配置参数
    parser.add_argument("--show-config", action="store_true", help="显示当前配置")

    # 强制处理文件参数
    parser.add_argument("--process", type=str, help="强制处理指定的文件")

    # 版本信息参数
    parser.add_argument("-v", "--version", action="store_true", help="显示版本信息")

    # Web 管理后台参数
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动 Web 管理后台（可与监控模式同时运行）",
    )
    parser.add_argument(
        "--web-host",
        type=str,
        default="0.0.0.0",
        help="Web 服务监听地址（默认: 0.0.0.0）",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web 服务监听端口（默认: 8080）",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="仅启动 Web 管理后台，不启动文件监控",
    )
    parser.add_argument(
        "--web-reload",
        action="store_true",
        help="启用 Web 服务热重载（开发模式）",
    )

    return parser


def parse_cli_args(args=None):
    """
    解析命令行参数的便捷函数

    Args:
        args: 要解析的参数列表, 如果为None则使用sys.argv[1:]

    Returns:
        解析后的参数
    """
    parser = get_cli_parser()
    return parser.parse_args(args)
