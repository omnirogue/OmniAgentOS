"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { intakeApi, type ClarifyResult, type ClarifyTurn } from "./client";
import { useSpeechDictation } from "./useSpeechDictation";
import { useSpeechSynthesis } from "./useSpeechSynthesis";
import { type TtsProvider, useTtsBackend } from "./useTtsBackend";

export type VoiceConversationState = "idle" | "listening" | "thinking" | "speaking" | "unsupported";
export type ConversationSpeaker = "user" | "fable";

export interface ConversationMessage {
  id: number;
  speaker: ConversationSpeaker;
  text: string;
}

export interface UseVoiceConversationOptions {
  enabled: boolean;
  outputProvider: TtsProvider;
}

export interface UseVoiceConversationResult {
  state: VoiceConversationState;
  messages: ConversationMessage[];
  interim: string;
  errorMessage: string | null;
  speechRecognitionSupported: boolean;
  speechSynthesisSupported: boolean;
  muted: boolean;
  paused: boolean;
  toggleListening: () => void;
  toggleMuted: () => void;
  clear: () => void;
}

function responseText(result: ClarifyResult): string {
  if (result.mode === "questions" && result.questions.length > 0) {
    return result.questions.length === 1
      ? result.questions[0]!
      : `I have a few quick questions. ${result.questions.map((question, index) => `${index + 1}. ${question}`).join(" ")}`;
  }
  if (result.spec) {
    return `I have enough detail to prepare this. ${result.spec.title}. ${result.spec.description}`;
  }
  return "I didn't get a usable response from intake. Please try saying that again.";
}

/**
 * Drives the hands-free loop: capture a final speech chunk, ask Fable through
 * the existing token-safe intake client, speak its reply, then re-open the mic.
 */
export function useVoiceConversation({ enabled, outputProvider }: UseVoiceConversationOptions): UseVoiceConversationResult {
  const [state, setState] = useState<VoiceConversationState>("idle");
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);
  const [paused, setPaused] = useState(false);
  const sendingRef = useRef(false);
  const historyRef = useRef<ClarifyTurn[]>([]);
  const pendingQuestionsRef = useRef<string[]>([]);
  const messageIdRef = useRef(0);
  const activeRequestRef = useRef(0);

  const speech = useSpeechSynthesis();
  const backendTts = useTtsBackend();

  const addMessage = useCallback((speaker: ConversationSpeaker, text: string) => {
    setMessages((current) => [...current, { id: ++messageIdRef.current, speaker, text }]);
  }, []);

  const onFinalResult = useCallback(async (utterance: string) => {
    if (!enabled || muted || paused || sendingRef.current || !utterance.trim()) return;
    sendingRef.current = true;
    const requestId = ++activeRequestRef.current;
    setState("thinking");
    setErrorMessage(null);
    addMessage("user", utterance);

    // The intake contract stores question/answer turns. A spoken reply answers
    // Fable's most recent set of questions, while the original utterance stays
    // the draft for this turn, preserving context without changing the API.
    const history = pendingQuestionsRef.current.length
      ? [...historyRef.current, ...pendingQuestionsRef.current.map((q) => ({ q, a: utterance }))]
      : historyRef.current;
    try {
      const result = await intakeApi.clarify(utterance, history);
      if (requestId !== activeRequestRef.current || !enabled) return;
      historyRef.current = history;
      pendingQuestionsRef.current = result.mode === "questions" ? result.questions : [];
      const reply = responseText(result);
      addMessage("fable", reply);
      setState("speaking");
      const playedByBackend = await backendTts.play(reply, outputProvider);
      if (!playedByBackend) await speech.speak(reply);
    } catch (reason) {
      if (requestId !== activeRequestRef.current || !enabled) return;
      const message = reason instanceof Error ? reason.message : "Couldn't reach Fable.";
      setErrorMessage(`Fable couldn't respond: ${message}`);
      addMessage("fable", "I couldn't reach intake just then. Please try again.");
    } finally {
      if (requestId === activeRequestRef.current) {
        sendingRef.current = false;
        setState("idle");
      }
    }
  }, [addMessage, backendTts, enabled, muted, outputProvider, paused, speech]);

  const dictation = useSpeechDictation(onFinalResult);
  const {
    supported: speechRecognitionSupported,
    listening,
    interim,
    errorMessage: dictationErrorMessage,
    start: startDictation,
    stop: stopDictation,
  } = dictation;

  // Stop capture as soon as a final chunk starts an intake call. This prevents
  // the microphone from hearing Fable's own reply and creating a feedback loop.
  useEffect(() => {
    if (state === "thinking" || state === "speaking" || muted || paused || !enabled) stopDictation();
  }, [enabled, muted, paused, state, stopDictation]);

  // Automatic listen-again happens only after speech is fully finished and only
  // when the operator hasn't deliberately paused or muted the conversation.
  useEffect(() => {
    if (!enabled) return;
    if (!speechRecognitionSupported) {
      setState("unsupported");
      return;
    }
    // Browser capability can be detected after mount. Clear the temporary
    // unsupported state before resuming the normal listen cycle.
    if (state === "unsupported") {
      setState("idle");
      return;
    }
    if (!muted && !paused && state === "idle") startDictation();
  }, [enabled, muted, paused, speechRecognitionSupported, startDictation, state]);

  useEffect(() => {
    // A recognition session can still report `listening` for one render after
    // a final chunk. Never let that stale signal replace thinking/speaking.
    if (listening && (state === "idle" || state === "listening")) setState("listening");
  }, [listening, state]);

  useEffect(() => {
    if (!enabled) {
      activeRequestRef.current += 1;
      sendingRef.current = false;
      stopDictation();
      speech.cancel();
      backendTts.stop();
      setState("idle");
    }
  }, [backendTts, enabled, speech, stopDictation]);

  const toggleListening = useCallback(() => {
    if (!speechRecognitionSupported) return;
    if (listening) {
      setPaused(true);
      stopDictation();
      setState("idle");
    } else if (state === "thinking" || state === "speaking") {
      // This control is deliberately mic-only: pausing while Fable is working
      // or speaking prevents the next listen cycle without cutting the reply.
      setPaused(true);
    } else {
      setMuted(false);
      setPaused(false);
      setState("idle");
    }
  }, [listening, speechRecognitionSupported, state, stopDictation]);

  const toggleMuted = useCallback(() => {
    setMuted((current) => {
      if (!current) stopDictation();
      return !current;
    });
  }, [stopDictation]);

  const clear = useCallback(() => {
    activeRequestRef.current += 1;
    sendingRef.current = false;
    historyRef.current = [];
    pendingQuestionsRef.current = [];
    setMessages([]);
    setErrorMessage(null);
    speech.cancel();
    backendTts.stop();
    setState("idle");
  }, [backendTts, speech]);

  return {
    state,
    messages,
    interim,
    errorMessage: errorMessage ?? dictationErrorMessage,
    speechRecognitionSupported,
    speechSynthesisSupported: speech.supported,
    muted,
    paused,
    toggleListening,
    toggleMuted,
    clear,
  };
}
