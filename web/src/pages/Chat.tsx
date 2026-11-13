import { useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import {
  api,
  type AttachmentInfo,
  type ChatMessage,
  type ConversationInfo,
  type PendingAction,
} from "../api";
import { prepareImage, startRecording, type Recording } from "../media";

type Draft = Omit<ChatMessage, "id"> & { id: number | string };
type Voice = "idle" | "recording" | "transcribing";

export default function Chat({ draft = "", onDraftUsed }: { draft?: string; onDraftUsed?: () => void }) {
  const [conversations, setConversations] = useState<ConversationInfo[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Draft[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showList, setShowList] = useState(false);
  const [actions, setActions] = useState<Record<number, PendingAction>>({});
  const [images, setImages] = useState<AttachmentInfo[]>([]);
  const [uploading, setUploading] = useState(0);
  const [voice, setVoice] = useState<Voice>("idle");
  const [caps, setCaps] = useState({ stt: false, tts: false });
  const recording = useRef<Recording | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const loadConversations = () => api.conversations().then(setConversations).catch(() => {});

  useEffect(() => {
    loadConversations();
    api
      .settings()
      .then((s) => setCaps(s.capabilities))
      .catch(() => {});
    return () => recording.current?.cancel();
  }, []);

  useEffect(() => {
    // Arrived from the inbox ("Draft a reply"): a fresh conversation with the request
    // typed in, left for the user to send.
    if (!draft) return;
    setActiveId(null);
    setMessages([]);
    setActions({});
    setText(draft);
    onDraftUsed?.();
  }, [draft, onDraftUsed]);

  async function addImages(files: File[]) {
    const chosen = files.filter((f) => f.type.startsWith("image/"));
    if (!chosen.length) return;
    setError("");
    setUploading((n) => n + chosen.length);
    for (const file of chosen) {
      try {
        const info = await api.uploadImage(await prepareImage(file), file.name || "image");
        setImages((list) => [...list, info]);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files);
    if (files.some((f) => f.type.startsWith("image/"))) {
      e.preventDefault();
      addImages(files);
    }
  }

  async function toggleVoice() {
    setError("");
    try {
      if (voice === "idle") {
        recording.current = await startRecording();
        setVoice("recording");
      } else if (voice === "recording" && recording.current) {
        setVoice("transcribing");
        const audio = await recording.current.stop();
        recording.current = null;
        const heard = (await api.transcribe(audio)).text;
        if (heard) setText((t) => (t.trim() ? `${t.trim()} ${heard}` : heard));
        else setError("I couldn't make out any words. Try again a little closer to the microphone.");
        setVoice("idle");
      }
    } catch (e) {
      recording.current = null;
      setVoice("idle");
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages, busy]);

  async function open(id: number | null) {
    setActiveId(id);
    setError("");
    setShowList(false);
    setMessages(id === null ? [] : await api.messages(id));
    await loadActions(id);
  }

  async function loadActions(id: number | null) {
    const list = id === null ? [] : await api.actions(id);
    setActions(Object.fromEntries(list.map((a) => [a.id, a])));
  }

  async function resolve(actionId: number, approve: boolean) {
    setError("");
    try {
      await (approve ? api.approveAction(actionId) : api.rejectAction(actionId));
    } catch (e) {
      setError((e as Error).message);
    }
    if (activeId !== null) {
      setMessages(await api.messages(activeId));
      await loadActions(activeId);
    }
  }

  async function remove(id: number) {
    if (!confirm("Delete this conversation?")) return;
    await api.deleteConversation(id);
    if (id === activeId) await open(null);
    loadConversations();
  }

  async function send() {
    const body = text.trim();
    const attached = images;
    if ((!body && !attached.length) || busy || uploading > 0) return;
    setText("");
    setImages([]);
    setError("");
    setBusy(true);
    setMessages((m) => [
      ...m,
      { id: `pending-${Date.now()}`, role: "user", content: body, meta: { attachments: attached } },
    ]);
    try {
      const res = await api.chat(
        body,
        activeId,
        attached.map((a) => a.id),
      );
      setActiveId(res.conversation_id);
      setMessages((m) => [...m, res.message]);
      loadConversations();
      if (res.message.meta?.tool_events?.some((e) => e.pending_action_id)) {
        await loadActions(res.conversation_id);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="flex h-full">
      <aside
        className={`${showList ? "flex" : "hidden"} w-full shrink-0 flex-col border-r border-slate-800 sm:flex sm:w-64`}
      >
        <button className="btn m-3" onClick={() => open(null)}>
          New conversation
        </button>
        <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {conversations.map((c) => (
            <li key={c.id} className="group flex items-center">
              <button
                onClick={() => open(c.id)}
                className={`min-w-0 flex-1 truncate rounded-lg px-3 py-2 text-left text-sm ${
                  c.id === activeId ? "bg-slate-800" : "text-slate-300 hover:bg-slate-900"
                }`}
              >
                {c.title}
              </button>
              <button
                aria-label="Delete conversation"
                onClick={() => remove(c.id)}
                className="px-2 text-slate-500 hover:text-red-400 sm:invisible sm:group-hover:visible"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className={`${showList ? "hidden" : "flex"} min-w-0 flex-1 flex-col sm:flex`}>
        <button
          className="border-b border-slate-800 px-4 py-2 text-left text-sm text-slate-400 sm:hidden"
          onClick={() => setShowList(true)}
        >
          ← Conversations
        </button>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-4 p-4">
            {messages.length === 0 && !busy && (
              <p className="pt-16 text-center text-slate-500">
                Ask anything, or tell me something to remember.
              </p>
            )}
            {messages.map((m) => (
              <Bubble key={m.id} message={m} actions={actions} onResolve={resolve} canListen={caps.tts} />
            ))}
            {busy && <p className="animate-pulse text-sm text-slate-500">Thinking…</p>}
            {error && (
              <p className="rounded-lg border border-red-900 bg-red-950/50 p-3 text-sm text-red-300">
                {error}
              </p>
            )}
            <div ref={bottom} />
          </div>
        </div>
        <div className="border-t border-slate-800 p-3">
          <div className="mx-auto max-w-3xl space-y-2">
            {(images.length > 0 || uploading > 0) && (
              <div className="flex flex-wrap gap-2">
                {images.map((a) => (
                  <div key={a.id} className="relative">
                    <img
                      src={`/api/attachments/${a.id}`}
                      alt={a.filename}
                      className="h-16 w-16 rounded-lg border border-slate-700 object-cover"
                    />
                    <button
                      aria-label={`Remove ${a.filename}`}
                      className="absolute -right-1.5 -top-1.5 h-5 w-5 rounded-full bg-slate-700 text-xs leading-none text-white hover:bg-red-600"
                      onClick={() => setImages((list) => list.filter((x) => x.id !== a.id))}
                    >
                      ×
                    </button>
                  </div>
                ))}
                {uploading > 0 && <span className="self-center text-xs text-slate-400">Uploading…</span>}
              </div>
            )}
            {voice !== "idle" && (
              <p className="text-xs text-slate-400">
                {voice === "recording" ? "Listening… press Stop when you are done." : "Writing down what you said…"}
              </p>
            )}
            <div className="flex items-end gap-2">
              <input
                ref={picker}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(e) => {
                  addImages(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                className="btn-ghost shrink-0"
                title="Attach a photo or screenshot (you can also paste one)"
                onClick={() => picker.current?.click()}
              >
                Image
              </button>
              {caps.stt && (
                <button
                  className={`shrink-0 ${voice === "recording" ? "btn bg-red-600 hover:bg-red-500" : "btn-ghost"}`}
                  disabled={voice === "transcribing"}
                  onClick={toggleVoice}
                >
                  {voice === "recording" ? "Stop" : "Speak"}
                </button>
              )}
              <textarea
                className="input max-h-40 min-h-[2.5rem] resize-y"
                rows={1}
                placeholder="Message"
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={onKey}
                onPaste={onPaste}
              />
              <button
                className="btn shrink-0"
                onClick={send}
                disabled={busy || uploading > 0 || (!text.trim() && !images.length)}
              >
                Send
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

function ActionCard({
  action,
  onResolve,
}: {
  action: PendingAction;
  onResolve: (id: number, approve: boolean) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const outcome: Record<string, string> = {
    done: "Approved and done",
    failed: "Approved, but it failed",
    rejected: "Rejected",
    running: "Running…",
  };
  const decide = async (approve: boolean) => {
    setBusy(true);
    await onResolve(action.id, approve);
    setBusy(false);
  };
  return (
    <div className="mt-2 rounded-xl border border-amber-800/60 bg-amber-950/30 p-3 text-sm">
      <p className="font-medium text-amber-200">
        {action.status === "pending" ? "Needs your approval" : outcome[action.status]}:{" "}
        <span className="font-mono">{action.tool}</span>
      </p>
      <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs text-slate-300">
        {JSON.stringify(action.arguments, null, 2)}
      </pre>
      {action.status === "pending" ? (
        <div className="mt-2 flex gap-2">
          <button className="btn" disabled={busy} onClick={() => decide(true)}>
            Approve
          </button>
          <button className="btn-ghost" disabled={busy} onClick={() => decide(false)}>
            Reject
          </button>
        </div>
      ) : (
        action.status === "failed" && <p className="mt-2 text-xs text-red-300">{action.result}</p>
      )}
    </div>
  );
}

function Bubble({
  message,
  actions,
  onResolve,
  canListen,
}: {
  message: Draft;
  actions: Record<number, PendingAction>;
  onResolve: (id: number, approve: boolean) => Promise<void>;
  canListen: boolean;
}) {
  const mine = message.role === "user";
  const attachments = message.meta?.attachments ?? [];
  const [audio, setAudio] = useState<HTMLAudioElement | "loading" | null>(null);
  const [speechError, setSpeechError] = useState("");

  useEffect(() => () => void (audio instanceof HTMLAudioElement && audio.pause()), [audio]);

  async function listen() {
    if (audio instanceof HTMLAudioElement) {
      audio.pause();
      setAudio(null);
      return;
    }
    setSpeechError("");
    setAudio("loading");
    try {
      const url = URL.createObjectURL(await api.speak(message.content));
      const player = new Audio(url);
      player.onended = () => {
        URL.revokeObjectURL(url);
        setAudio(null);
      };
      await player.play();
      setAudio(player);
    } catch (e) {
      setAudio(null);
      setSpeechError((e as Error).message);
    }
  }

  const events = message.meta?.tool_events ?? [];
  const held = events.flatMap((e) =>
    e.pending_action_id && actions[e.pending_action_id] ? [actions[e.pending_action_id]] : [],
  );
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div className="max-w-[85%]">
        {events.length > 0 && (
          <div className="mb-1 flex flex-wrap gap-1">
            {events.map((e, i) => (
              <span
                key={i}
                title={JSON.stringify(e.arguments)}
                className={`rounded-full px-2 py-0.5 text-xs ${
                  e.ok ? "bg-slate-800 text-slate-300" : "bg-red-950 text-red-300"
                }`}
              >
                {e.tool}
                {e.pending_action_id ? " (held)" : e.ok ? "" : " failed"}
              </span>
            ))}
          </div>
        )}
        {attachments.length > 0 && (
          <div className={`mb-1 flex flex-wrap gap-2 ${mine ? "justify-end" : ""}`}>
            {attachments.map((a) => (
              <a key={a.id} href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer">
                <img
                  src={`/api/attachments/${a.id}`}
                  alt={a.filename}
                  className="max-h-48 max-w-full rounded-xl border border-slate-700 object-contain"
                />
              </a>
            ))}
          </div>
        )}
        {message.content && (
          <div
            className={`whitespace-pre-wrap break-words rounded-2xl px-4 py-2 text-sm leading-relaxed ${
              mine ? "bg-sky-700 text-white" : "bg-slate-800 text-slate-100"
            }`}
          >
            {message.content}
          </div>
        )}
        {!mine && canListen && message.content && (
          <button
            className="mt-1 text-xs text-slate-400 hover:text-slate-200"
            disabled={audio === "loading"}
            onClick={listen}
          >
            {audio === "loading" ? "Preparing audio…" : audio ? "Stop" : "Listen"}
          </button>
        )}
        {speechError && <p className="mt-1 text-xs text-red-400">{speechError}</p>}
        {held.map((a) => (
          <ActionCard key={a.id} action={a} onResolve={onResolve} />
        ))}
        {message.meta?.used_fallback && (
          <p className="mt-1 text-xs text-amber-400">Answered by fallback model {message.meta.model}</p>
        )}
      </div>
    </div>
  );
}
