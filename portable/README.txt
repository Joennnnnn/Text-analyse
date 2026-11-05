这个目录里是构建好的 Windows 离线包，不需要联网、不需要 Python、不需要构建。

两种用法：

1) 双击仓库根目录的 run.cmd
   第一次会自动把 chipdoc-offline.tar.gz 解压到本目录，然后运行。

2) 手动解压
   tar -xzf chipdoc-offline.tar.gz
   chipdoc-offline\run.cmd where

注意：包内是 Windows x64 的 Python 与 PyMuPDF，只能在 64 位 Windows 上运行。
