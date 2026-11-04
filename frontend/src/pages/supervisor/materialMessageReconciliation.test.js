import assert from "node:assert/strict";
import test from "node:test";
import {
  appendMaterialUploadMessages,
  reconcileAssistantMessage,
} from "./materialMessageReconciliation.js";

test("upload submission appends the user file message before processing feedback", () => {
  const previous = { id: "previous", role: "assistant", text: "请上传资料" };

  assert.deepEqual(
    appendMaterialUploadMessages([previous], "parse-1", "资料.docx"),
    [
      previous,
      { id: "parse-1-user", role: "user", text: "我上传了一份资料：资料.docx。" },
      { id: "parse-1", role: "assistant", text: "正在解析文档，请稍候。" },
    ],
  );
});

test("final material response replaces the streamed answer instead of duplicating it", () => {
  const summary = { id: "summary", role: "assistant", text: "文档摘要" };
  const streamed = {
    id: "answer-stream",
    role: "assistant",
    text: "流式草稿内容",
  };
  const finalMessage = {
    id: "final-result",
    role: "assistant",
    text: "已生成草稿",
    response: { status: "DRAFT_GENERATED" },
    elapsedMs: 1200,
  };

  const result = reconcileAssistantMessage(
    [summary, streamed],
    new Set(["answer-stream"]),
    finalMessage,
  );

  assert.deepEqual(result, [
    summary,
    {
      ...streamed,
      ...finalMessage,
      id: streamed.id,
    },
  ]);
});

test("final material response is appended when no answer was streamed", () => {
  const existing = { id: "summary", role: "assistant", text: "文档摘要" };
  const finalMessage = {
    id: "final-result",
    role: "assistant",
    text: "解析完成",
  };

  assert.deepEqual(
    reconcileAssistantMessage([existing], new Set(), finalMessage),
    [existing, finalMessage],
  );
});
