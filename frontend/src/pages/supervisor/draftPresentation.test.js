import assert from "node:assert/strict";
import test from "node:test";
import {
  applyDraftOverrides,
  detachDraftAttachment,
  normalizeSolutionDraftFields,
  projectDraftFields,
} from "./draftPresentation.js";
import { examples } from "../../config/quickIntents.js";
import { displayDraftField } from "../../draftFieldLabels.js";

test("方案草稿只保留发布企业字段并合并公司名称值", () => {
  const normalized = normalizeSolutionDraftFields(
    { pub_dept: "", company: "深圳市凯裕福科技有限公司" },
    { company: "深圳市凯裕福科技有限公司" },
    "身份企业",
  );

  assert.deepEqual(normalized.fields, { pub_dept: "深圳市凯裕福科技有限公司" });
  assert.deepEqual(normalized.mysqlFields, {
    pub_dept: "深圳市凯裕福科技有限公司",
  });
});

test("方案草稿没有企业值时使用身份企业填充发布企业名称", () => {
  assert.deepEqual(normalizeSolutionDraftFields({}, {}, "身份企业"), {
    fields: { pub_dept: "身份企业" },
    mysqlFields: { pub_dept: "身份企业" },
  });
});

test("方案草稿的空白发布字段不会遮蔽公司字段值", () => {
  assert.deepEqual(
    normalizeSolutionDraftFields(
      { pub_dept: "  ", company: "方案单位" },
      {},
      "身份企业",
    ),
    { fields: { pub_dept: "方案单位" }, mysqlFields: { pub_dept: "方案单位" } },
  );
});

test("首页三个固定问题作为普通消息展示，不携带入口路由参数", () => {
  assert.deepEqual(examples, [
    "我有创新需求，帮我匹配专家或成果",
    "我有技术成果，帮我寻找潜在客户",
    "我有智能化升级需求，帮我推荐方案商",
  ]);
});

test("草稿表单数据和持久化字段都不保留合同外字段", () => {
  assert.deepEqual(
    projectDraftFields(
      { title: "方案", company: "不应单独保留", internal: "x" },
      { pub_dept: "单位", mysql_extra: "x" },
      ["title", "pub_dept"],
    ),
    { fields: { title: "方案" }, mysqlFields: { pub_dept: "单位" } },
  );
});

test("移除附件只解除当前草稿关联并清除当前草稿解析块", () => {
  const original = {
    attachments: [
      { document_id: "doc-1", file_name: "资料.docx" },
      { document_id: "doc-2", file_name: "附件.pdf" },
    ],
    source_blocks: [{ text: "当前草稿的解析内容" }],
    title: "草稿内容",
  };

  assert.deepEqual(
    detachDraftAttachment(original, original.attachments[0]),
    {
      attachments: [{ document_id: "doc-2", file_name: "附件.pdf" }],
      source_blocks: [],
      title: "草稿内容",
    },
  );
  assert.equal(original.attachments.length, 2);
});

test("分页草稿应用本会话变更，翻页后不会恢复旧附件", () => {
  const drafts = [
    { draft_id: "draft-1", title: "需求", attachments: [{ document_id: "doc-1" }] },
    { draft_id: "draft-2", title: "方案", attachments: [{ document_id: "doc-1" }] },
  ];
  const visible = applyDraftOverrides(drafts, {
    "draft-1": { attachments: [], version: 3 },
  });

  assert.deepEqual(visible[0], { ...drafts[0], attachments: [], version: 3 });
  assert.deepEqual(visible[1], drafts[1]);
});

test("方案和成果企业字段按草稿合同语义显示", () => {
  assert.equal(displayDraftField("pub_dept", "solution"), "发布企业名称");
  assert.equal(displayDraftField("company", "achievement"), "发布单位");
  assert.equal(displayDraftField("category", "achievement"), "成果类型");
  assert.equal(displayDraftField("analyse_contect", "achievement"), "成果分析");
});
