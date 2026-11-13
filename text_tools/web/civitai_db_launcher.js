import { app } from "../../scripts/app.js";

const PANEL_URL = "/db/";

function openPanel(hash = "") {
  window.open(PANEL_URL + hash, "_blank", "noopener");
}

app.registerExtension({
  name: "civitai.db.launcher",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name === "CivitaiDBPanel") {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
        this.addWidget("button", "打开 Civitai 数据库面板", null, () => openPanel());
        this.addWidget("button", "打开 PNG 注入", null, () => openPanel("#inject"));
        return result;
      };
    }
    if (nodeData.name === "CivitaiInjectImages") {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
        this.addWidget("button", "打开面板 · PNG 注入", null, () => openPanel("#inject"));
        return result;
      };
    }
  },
  async setup() {
    // 顶部菜单不是所有前端版本都有；找不到就只保留节点按钮。
    const addMenuButton = () => {
      const menu = document.querySelector(".comfy-menu");
      if (!menu) return false;
      if (document.getElementById("civitai-db-menu-button")) return true;
      const button = document.createElement("button");
      button.id = "civitai-db-menu-button";
      button.textContent = "Civitai 数据库";
      button.title = "打开 Civitai 数据库管理面板";
      button.style.cssText = "margin:4px;padding:4px 8px;";
      button.addEventListener("click", () => openPanel());
      menu.appendChild(button);
      return true;
    };
    if (!addMenuButton()) setTimeout(addMenuButton, 1500);
  },
});
