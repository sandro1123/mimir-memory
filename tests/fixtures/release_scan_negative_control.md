# 负向对照件（P0-C 闸门自检）

本文件故意携带一份假 secret 与内网地址——扫描器必须每次都抓住它。
若 CI 的 release-scan job 没有在本文件上报 HIT，说明闸门已静默失效。

- 假密钥：sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE000
- 假内网：192.168.5.99
