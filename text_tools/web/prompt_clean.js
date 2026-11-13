// text_tools 节点前端脚本
//
// PromptClean：
//   * 节点里并排放两个文本框：左边输入（可编辑），右边输出（只读，执行后显示清洗结果）。
//     ComfyUI 原生控件是“整行、竖排”的，没有左右并排的 API；这里用一个自定义 DOM 控件
//     把两个 textarea 画在同一行里，并把原生的 text 控件隐藏起来继续当数据源
//     （隐藏的 text 控件仍然参与工作流保存，也仍然作为 text 输入送去排队）。
//   * 老工作流里这个节点只存了 2 个输出，这里会补上第 3 个 conditioning。
//
// PromptCleanEncode：
//   * 保持原样：text 输入 + 节点内只读预览。
import { app } from "../../scripts/app.js";

const CLEAN_NODE = "PromptClean";
const ENCODE_NODE = "PromptCleanEncode";
const NODE_TYPES = new Set([CLEAN_NODE, ENCODE_NODE]);
const BOXES_WIDGET = "prompt_boxes";
const PREVIEW_WIDGET = "cleaned_text";
const STRING_OUTPUT_SLOT = 0;

const MONO_FONT = "ui-monospace, Consolas, 'Courier New', monospace";
const BOX_STYLE = [
  "flex:1 1 auto",
  "min-height:0",
  "width:100%",
  "box-sizing:border-box",
  "resize:none",
  "padding:6px",
  "line-height:1.35",
  `font-family:${MONO_FONT}`,
  "font-size:12px",
  "white-space:pre-wrap",
  "overflow:auto",
  "border:1px solid var(--border-color, #555)",
  "border-radius:6px",
  "background:var(--comfy-input-bg, rgba(0, 0, 0, 0.25))",
  "color:var(--input-text, #e6e6e6)",
].join(";");

function toText(raw) {
  if (raw == null) return "";
  if (Array.isArray(raw)) {
    const parts = [];
    for (const item of raw) {
      if (item == null) continue;
      parts.push(Array.isArray(item) ? item.join("\n") : String(item));
    }
    return parts.join("\n");
  }
  return String(raw);
}

/** 老工作流只存了部分输出时，按节点定义补齐（主要是第 3 个 conditioning）。 */
function ensureOutputs(node, nodeData) {
  const types = nodeData?.output;
  if (!Array.isArray(types)) return;
  const names = Array.isArray(nodeData.output_name) ? nodeData.output_name : [];
  for (let i = 0; i < types.length; i++) {
    if (node.outputs?.[i]) continue;
    try {
      node.addOutput(names[i] ?? `output${i}`, types[i] ?? "*");
    } catch (error) {
      console.warn("[PromptClean] 补齐输出失败:", error);
    }
  }
}

/* ------------------------------ PromptClean ------------------------------ */

/** 懒创建“左输入 / 右输出”的并排文本框。 */
function buildBoxes(node) {
  if (node._pcBoxes) return node._pcBoxes;
  if (typeof node.addDOMWidget !== "function") return null;

  // 原生 text 控件：继续作为数据源，只是不显示
  const textWidget = (node.widgets ?? []).find((w) => w.name === "text");
  if (!textWidget) return null;

  const wrap = document.createElement("div");
  wrap.style.cssText =
    "display:flex;gap:10px;width:100%;height:100%;min-height:0;";

  const makeColumn = (caption, readOnly, placeholder) => {
    const column = document.createElement("div");
    column.style.cssText =
      "flex:1 1 0;min-width:0;height:100%;display:flex;flex-direction:column;gap:4px;";

    const label = document.createElement("div");
    label.textContent = caption;
    label.style.cssText = "flex:0 0 auto;font-size:11px;line-height:1;opacity:.75;";
    column.appendChild(label);

    const area = document.createElement("textarea");
    area.className = "comfy-multiline-input";
    area.readOnly = readOnly;
    area.spellcheck = false;
    area.placeholder = placeholder;
    area.style.cssText = BOX_STYLE;
    column.appendChild(area);

    wrap.appendChild(column);
    return area;
  };

  const left = makeColumn("输入（原始提示词）", false, "在这里输入原始提示词");
  const right = makeColumn("输出（清洗后）", true, "运行后显示清洗后的提示词");

  const widget = node.addDOMWidget(BOXES_WIDGET, "customtext", wrap, {
    serialize: false,
    hideOnZoom: false,
    getValue: () => left.value,
    setValue: (value) => {
      left.value = value == null ? "" : String(value);
    },
    getMinHeight: () => 170,
    getMaxHeight: () => 100000,
  });
  widget.options.serialize = false;
  widget.serialize = false;

  textWidget.hidden = true;

  const push = () => {
    const value = left.value;
    if (textWidget.value !== value) {
      textWidget.value = value;
      textWidget.callback?.(value);
    }
  };
  const pull = () => {
    if (document.activeElement === left) return;
    const value = textWidget.value == null ? "" : String(textWidget.value);
    if (left.value !== value) left.value = value;
  };
  left.addEventListener("input", () => {
    push();
    node.setDirtyCanvas?.(true, true);
  });

  node._pcBoxes = { widget, left, right, textWidget, push, pull };
  pull();
  try {
    node.arrange?.();
  } catch (error) {
    console.warn("[PromptClean] 重新布局失败:", error);
  }
  return node._pcBoxes;
}

function setBoxesOutput(node, value) {
  const boxes = node._pcBoxes ?? buildBoxes(node);
  if (!boxes) return;
  boxes.right.value = value;
  node.setDirtyCanvas?.(true, true);
}

/* --------------------------- PromptCleanEncode --------------------------- */

function ensurePreview(node) {
  if (node._promptCleanPreview) return node._promptCleanPreview;
  if (typeof node.addDOMWidget !== "function") {
    console.warn("[PromptClean] 当前前端不支持 addDOMWidget，跳过节点内文本预览");
    return null;
  }

  const el = document.createElement("textarea");
  el.readOnly = true;
  el.spellcheck = false;
  el.placeholder = "运行后在此显示清洗后的文本";
  el.className = "comfy-multiline-input";
  el.style.cssText = BOX_STYLE;

  const widget = node.addDOMWidget(PREVIEW_WIDGET, "customtext", el, {
    serialize: false,
    hideOnZoom: false,
    getValue: () => el.value,
    setValue: (value) => {
      el.value = value == null ? "" : String(value);
    },
    getMinHeight: () => 72,
    getMaxHeight: () => 260,
  });
  widget.options.serialize = false;
  widget.serialize = false;

  node._promptCleanPreview = widget;
  return widget;
}

function setPreview(node, value) {
  const widget = ensurePreview(node);
  if (!widget) return;
  widget.value = value;
  node.setDirtyCanvas?.(true, true);
}

/* ---------------------------- 下游控件同步 -------------------------------- */

function forEachOutgoingLink(node, slot, callback) {
  const graph = node.graph ?? app.graph;
  if (!graph) return;
  const nodes = graph._nodes ?? graph.nodes ?? [];
  const links = graph.links;
  const getLink = (id) => (typeof links?.get === "function" ? links.get(id) : links?.[id]);

  for (const target of nodes) {
    if (!target?.inputs) continue;
    for (const input of target.inputs) {
      if (input?.link == null) continue;
      const link = getLink(input.link);
      if (!link || link.origin_id !== node.id || link.origin_slot !== slot) continue;
      callback(target, input);
    }
  }
}

function syncToLinkedWidgets(node, value) {
  if (typeof value !== "string" || value.length === 0) return;
  try {
    forEachOutgoingLink(node, STRING_OUTPUT_SLOT, (target, input) => {
      const widgetName = input.widget?.name;
      if (!widgetName) return;
      const widget = target.widgets?.find((w) => w.name === widgetName);
      if (!widget) return;
      try {
        widget.value = value;
        widget.callback?.(value);
        target.setDirtyCanvas?.(true, true);
      } catch (error) {
        console.warn("[PromptClean] 同步到下游控件失败:", error);
      }
    });
  } catch (error) {
    console.warn("[PromptClean] 查找下游连线失败:", error);
  }
}

/* --------------------------------- 注册 --------------------------------- */

app.registerExtension({
  name: "horizontalSight.PromptClean",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!NODE_TYPES.has(nodeData.name)) return;
    const isClean = nodeData.name === CLEAN_NODE;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      if (isClean) {
        buildBoxes(this);
        if (!app.configuringGraph) {
          try {
            const [width, height] = this.computeSize();
            this.setSize([Math.max(width, 760), Math.max(height, 320)]);
            this.arrange?.();
          } catch (error) {
            console.warn("[PromptClean] 调整节点尺寸失败:", error);
          }
        }
      } else {
        ensurePreview(this);
        if (!app.configuringGraph) {
          try {
            const [width, height] = this.computeSize();
            this.setSize([Math.max(width, 420), Math.max(height, 210)]);
          } catch (error) {
            console.warn("[PromptClean] 调整节点尺寸失败:", error);
          }
        }
      }
    };

    // 从工作流 JSON 载入时，onNodeCreated 不一定被调用，这里兜底。
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      onConfigure?.apply(this, arguments);
      ensureOutputs(this, nodeData);
      if (isClean) {
        buildBoxes(this);
        const node = this;
        requestAnimationFrame(() => {
          try {
            node._pcBoxes?.pull();
            node.arrange?.();
          } catch (error) {
            console.warn("[PromptClean] 载入后同步文本框失败:", error);
          }
        });
      } else {
        ensurePreview(this);
      }
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const value = toText(message?.text);
      if (isClean) {
        setBoxesOutput(this, value);
        syncToLinkedWidgets(this, value);
      } else {
        setPreview(this, value);
      }
    };
  },
});
