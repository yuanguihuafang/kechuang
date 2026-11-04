import { useEffect, useRef, useState } from 'react';

function browserRecognition() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function recognitionErrorMessage(error) {
  const messages = {
    'not-allowed': '浏览器没有语音识别权限，请允许麦克风访问。',
    'service-not-allowed': '浏览器语音识别服务不可用，请改用键盘输入。',
    'audio-capture': '没有检测到可用麦克风，请检查系统输入设备。',
    network: '浏览器语音识别网络不可用，请稍后重试。',
    'no-speech': '没有识别到语音，请靠近麦克风再试一次。',
  };
  return messages[error] || `语音识别失败（${error || '未知错误'}），请重试。`;
}

/** 当前使用浏览器实时转写；后端 ASR 接口保留给未来正式模型接入。 */
export function VoiceInput({ onTranscript, disabled = false }) {
  const recognitionRef = useRef(null);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState('');
  const Recognition = browserRecognition();

  useEffect(() => () => {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
  }, []);

  function stopRecognition() {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setRecording(false);
  }

  function startRecognition() {
    setError('');
    if (!Recognition) {
      setError('当前浏览器不支持实时语音识别，请使用 Chrome 或 Edge。');
      return;
    }
    const recognition = new Recognition();
    recognition.lang = 'zh-CN';
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal) {
          const text = result[0]?.transcript?.trim();
          if (text) onTranscript?.(text);
        }
      }
    };
    recognition.onerror = (event) => {
      if (event.error !== 'aborted') setError(recognitionErrorMessage(event.error));
      setRecording(false);
    };
    recognition.onend = () => {
      recognitionRef.current = null;
      setRecording(false);
    };
    recognitionRef.current = recognition;
    try {
      recognition.start();
      setRecording(true);
    } catch (requestError) {
      recognitionRef.current = null;
      setRecording(false);
      setError(`无法启动语音识别：${requestError.message}`);
    }
  }

  return <div className="voice-input" aria-label="语音输入">
    <button
      type="button"
      className={`composer-voice${recording ? ' is-recording' : ''}`}
      onClick={() => (recording ? stopRecognition() : startRecognition())}
      disabled={disabled}
      aria-label={recording ? '停止语音识别' : '开始语音识别'}
      title={recording ? '停止语音识别' : '使用浏览器语音识别'}
    >
      <span aria-hidden="true">{recording ? '■' : '◉'}</span>
      <span>{recording ? '识别中' : '语音'}</span>
    </button>
    {error && <span className="voice-input-error" role="status">{error}</span>}
  </div>;
}
