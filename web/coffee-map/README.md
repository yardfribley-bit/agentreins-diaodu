# 咖啡数字地图

天通苑咖啡馆价格对比的静态单页原型。Leaflet 画地图，底图用高德瓦片。打开 `index.html` 即可，不经过 Hub 的 `/task/` 服务。

Leaflet 1.9.4 放在同目录的 `leaflet/`（`leaflet.js`、`leaflet.css`、`images/`，以及 BSD-2-Clause 的 `LICENSE`）。页面引用 `./leaflet/leaflet.js` 和 `./leaflet/leaflet.css`，不依赖 unpkg，方便在中国大陆直接打开。

## 数据

- **瑞幸**：真实数据。2026-11-01 通过瑞幸官方只读接口查询（门店列表与官方价格）。页面里标为「位置真实 / 官方价真实」。
- **其余全部是示例数据**：其他咖啡馆，以及美团外卖、淘宝闪购、京东秒送、抖音团购上的价格。只用于展示效果，不代表实际价格或位置。

## 部署

线上地址是 `https://www.chuhaijian.com/coffee-map/`，由 Nginx 直接托管静态文件，不走 `/task/` 的 Python 进程。

把本目录整份复制到 Nginx 上已经对应 `/coffee-map/` 的那个目录，保持 `index.html` 和 `leaflet/` 的相对位置。复制后不用改 `/task/` 的配置，也不用重启 diaodu。例如在服务器上：

```bash
rsync -a web/coffee-map/ /path/to/nginx/coffee-map/
```

`/path/to/nginx/coffee-map/` 换成该站点里 `location /coffee-map/` 实际指向的目录。
