# 起爆点·选股雷达 —— 云端部署指南（手机随时可用）

## 这套东西解决什么问题

现在你手机上开的网页，数据在**你家电脑**上。电脑一关，手机就打不开。
把这个 `cloud` 文件夹传到 GitHub 后，程序会跑在 **GitHub 的服务器**上，
每天自动扫描、自动更新，你的手机用 **WiFi 或 4G/5G 都能打开**，跟电脑没关系了。

---

## 一、准备

1. 一个 GitHub 账号（免费注册：https://github.com/signup）
2. 新建一个仓库：点右上角 **+** → **New repository**
   - Repository name：`qibao-radar`
   - 选 **Public**（私有仓库不能用免费 Pages）
   - 勾选 **Add a README file**
   - 点 **Create repository**

---

## 二、上传文件（3 步）

### 第 1 步：上传网页文件

在仓库页面点 **Add file → Upload files**，
把 `cloud\docs\` 里的这 5 个文件拖进去：

```
index.html
manifest.json
sw.js
icon-192.png
icon-512.png
```

> 先不要传 data.json，它会被程序自动生成。
> 传完点 **Commit changes**。

然后把这些文件**移动到 docs 文件夹**：
- 进入仓库 → 点每个文件 → 右上角铅笔图标 → 在文件名前面加 `docs/` → Commit
- 或者上传时直接把 `docs` 文件夹整个拖进去

### 第 2 步：上传扫描程序

**Add file → Upload files**，把 `cloud\scan_cloud.py` 拖进去 → Commit。

### 第 3 步：创建自动任务

1. 点 **Add file → Create new file**
2. 文件名输入：`.github/workflows/update.yml`
   （输入 `/` 会自动建文件夹，一层一层输）
3. 把 `cloud\.github\workflows\update.yml` 的内容整段复制粘贴进去
4. 点 **Commit changes**

---

## 三、开启网页服务

1. 仓库页 → **Settings** → 左侧 **Pages**
2. **Source** 选 `Deploy from a branch`
3. **Branch** 选 `main`，目录选 **`/docs`**
4. 点 **Save**
5. 等 1~2 分钟，页面上方会出现网址：

```
https://你的用户名.github.io/qibao-radar/
```

---

## 四、手动跑一次，生成数据

1. 仓库页 → **Actions** 标签
2. 左边选 **update-radar** → 右边 **Run workflow** → 绿色按钮
3. 等 3~5 分钟（要抓 5000 多只股票的数据）
4. 刷新网页，就能看到信号了

---

## 五、手机上装成 App

1. 手机浏览器打开 `https://你的用户名.github.io/qibao-radar/`
2. **iPhone**：Safari → 分享按钮 → **添加到主屏幕**
3. **安卓**：Chrome → 右上角三个点 → **添加到主屏幕**

装好后桌面会有一个深色图标，点开是**全屏无地址栏**，跟 App 一样。
**WiFi 和流量都能用**，不需要电脑开着。

---

## 六、自动更新时间

工作流已经设好（北京时间）：

| 时间 | 任务 |
|---|---|
| 每天 **09:26** | 采集集合竞价抢筹数据 |
| 每天 **15:35** | 扫描全市场起爆信号 |

只跑周一到周五。GitHub 的定时任务偶尔会延迟几分钟，属于正常。

---

## 七、几个注意点

**1. GitHub 在国内访问速度**
大多数情况能开，偶尔慢。如果打不开，手机挂个加速器，或者用电脑先打开一次让它缓存。

**2. 数据抓取失败**
GitHub 服务器在国外，抓腾讯/新浪的行情接口一般没问题。如果某天 Actions 报错，
去 **Actions → 那次运行 → 日志** 里看，通常是接口临时抽风，第二天会自己恢复。

**3. 集合竞价那一条**
GitHub 的定时任务可能延迟 5~15 分钟，9:30 之后采到的就不是竞价数据了。
如果发现竞价板块一直是空的，就把竞价那条 cron 删掉，只用 15:35 的收盘扫描。

**4. 私有仓库不行**
免费的 GitHub Pages 只支持 Public 仓库。

---

## 八、本地那套还要不要留

要。本地版功能更全（有历史回测、数据可导出、竞价采集时间精准）。
云端版是"出门在外看一眼"的轻量版。

两者用的是同一套判定逻辑，信号应该一致。
