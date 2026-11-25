import { useState, useEffect, useCallback, useRef } from 'react';
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { X, Loader2, AlertCircle, Download, Copy, Upload } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import { api, type AutomationStatusResponse } from '@/lib/api';

interface SettingsPanelProps {
    onClose: () => void;
}

type AutomationStatus = AutomationStatusResponse['status'];

export function SettingsPanel({ onClose }: SettingsPanelProps) {
    const [status, setStatus] = useState<AutomationStatus>('idle');
    const [email, setEmail] = useState('');
    const [otp, setOtp] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [logs, setLogs] = useState<string[]>([]);
    const [activeTab, setActiveTab] = useState<'automation' | 'layout'>('automation');

    const [syncInfo, setSyncInfo] = useState<AutomationStatusResponse | null>(null);
    const [statusMessage, setStatusMessage] = useState('');
    const lastSnapshot = useRef('');
    const initialFailure = useRef<string | null>(null);
    const busy = loading || ['exporting', 'downloading', 'initializing'].includes(status);

    const [dailySyncTime, setDailySyncTime] = useState("09:00");

    useEffect(() => {
        // Fetch settings on mount
        api.getSettings()
            .then(data => {
                if (data.daily_sync_time) setDailySyncTime(data.daily_sync_time);
                if (data.email) setEmail(data.email);
            })
            .catch(err => console.error("Failed to fetch settings", err));
    }, []);

    const addLog = useCallback((msg: string) => {
        if (msg) setLogs(prev => [...prev.slice(-199), `[${new Date().toLocaleTimeString()}] ${msg}`]);
    }, []);

    const applyStatus = useCallback((data: AutomationStatusResponse) => {
        setSyncInfo(data);
        const snapshot = `${data.status}:${data.message || ''}`;
        const firstSnapshot = lastSnapshot.current === '';
        if (firstSnapshot && data.status === 'error') initialFailure.current = snapshot;
        if (!firstSnapshot && snapshot !== initialFailure.current) initialFailure.current = null;
        // Opening settings (including a tray-window reopen) must not present a
        // previous attempt as a new error. A new job transition clears this.
        const previousFailure = initialFailure.current === snapshot;
        setStatus(previousFailure ? 'idle' : data.status);
        setStatusMessage(previousFailure ? '' : data.message || '');
        setError(!previousFailure && data.status === 'error' ? data.message || 'Sync failed.' : null);
        if (snapshot !== lastSnapshot.current) {
            lastSnapshot.current = snapshot;
            if (!firstSnapshot) addLog(data.message || `Status: ${data.status}`);
        }
    }, [addLog]);

    useEffect(() => {
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            try {
                const data = await api.checkStatus();
                if (!cancelled) applyStatus(data);
            } catch (err) {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Cannot reach the app service.');
            } finally {
                if (!cancelled) timer = setTimeout(poll, 5000);
            }
        };
        void poll();
        return () => { cancelled = true; clearTimeout(timer); };
    }, [applyStatus]);

    const handleSaveSettings = async () => {
        setLoading(true);
        try {
            await api.saveSettings({ daily_sync_time: dailySyncTime, email });
            addLog(`Settings saved: Daily sync at ${dailySyncTime}`);
        } catch (err: any) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    const handleClearSession = async () => {
        if (!confirm("Are you sure you want to clear the login session? You will need to login again.")) return;

        setLoading(true);
        try {
            await api.clearSession();
            setStatus('idle');
            addLog("Session cleared.");
        } catch (err: any) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    const handleStartLogin = async () => {
        setLoading(true);
        setError(null);
        addLog(`Starting login for ${email}...`);
        try {
            // Auto-save settings to persist email
            await api.saveSettings({ daily_sync_time: dailySyncTime, email });

            const data = await api.startLogin(email);
            applyStatus(data);
        } catch (err: any) {
            setError(err.message);
            addLog(`Error: ${err.message}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSubmitOtp = async () => {
        setLoading(true);
        setError(null);
        addLog(`Submitting OTP...`);
        try {
            const data = await api.submitOtp(otp);
            applyStatus(data);
            if (data.status !== 'otp_needed') setOtp('');
        } catch (err: any) {
            setError(err.message);
            addLog(`Error: ${err.message}`);
        } finally {
            setLoading(false);
        }
    };

    const handleRequestExport = async () => {
        setLoading(true);
        setError(null);
        addLog(syncInfo?.export_pending ? 'Checking the pending export...' : 'Checking existing exports before requesting new data...');
        try {
            const data = await api.requestExport();
            applyStatus(data);
        } catch (err: any) {
            setError(err.message);
            addLog(`Error: ${err.message}`);
        } finally {
            setLoading(false);
        }
    };

    const handleDownload = async () => {
        setLoading(true);
        setError(null);
        addLog(`Downloading and ingesting data...`);
        try {
            const data = await api.downloadExport();
            applyStatus(data);
        } catch (err: any) {
            setError(err.message);
            addLog(`Error: ${err.message}`);
        } finally {
            setLoading(false);
        }
    };

    const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        if (!file) return;

        setLoading(true);
        setError(null);
        addLog(`Uploading ${file.name}...`);

        try {
            const data = await api.uploadZip(file);
            addLog(data.message || "Upload complete");
            const current = await api.checkStatus();
            applyStatus(current);
        } catch (err: any) {
            setError(err.message);
            addLog(`Error: ${err.message}`);
        } finally {
            setLoading(false);
            // Reset input
            event.target.value = '';
        }
    };

    return (
        <div className="w-[400px] border-l bg-card flex flex-col h-full">
            {/* Header */}
            <div className="p-6 border-b flex items-center justify-between">
                <h2 className="text-lg font-semibold">Settings</h2>
                <Button variant="ghost" size="icon" onClick={onClose}>
                    <X className="h-4 w-4" />
                </Button>
            </div>

            {/* Tabs */}
            <div className="flex border-b">
                <button
                    className={cn(
                        "flex-1 py-3 text-sm font-medium border-b-2 transition-colors",
                        activeTab === 'automation'
                            ? "border-primary text-primary"
                            : "border-transparent text-muted-foreground hover:text-foreground"
                    )}
                    onClick={() => setActiveTab('automation')}
                >
                    Automation
                </button>
                <button
                    className={cn(
                        "flex-1 py-3 text-sm font-medium border-b-2 transition-colors",
                        activeTab === 'layout'
                            ? "border-primary text-primary"
                            : "border-transparent text-muted-foreground hover:text-foreground"
                    )}
                    onClick={() => setActiveTab('layout')}
                >
                    Layout
                </button>
            </div>

            <div className="flex-1 p-6 space-y-6 overflow-y-auto">
                {activeTab === 'automation' && (
                    <>
                        {/* Automation Config */}
                        <div className="space-y-4">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Configuration</h3>
                            <div className="space-y-2">
                                <Label>Daily export check</Label>
                                <div className="flex gap-2">
                                    <Input
                                        type="time"
                                        value={dailySyncTime}
                                        onChange={e => setDailySyncTime(e.target.value)}
                                    />
                                    <Button onClick={handleSaveSettings} disabled={busy} variant="outline">
                                        Save
                                    </Button>
                                </div>
                            </div>
                        </div>

                        {/* Manual Actions */}
                        <div className="space-y-4">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Sync Status</h3>

                            {/* Status Indicator */}
                            <div className="flex items-center gap-2 p-3 bg-secondary/50 rounded-lg">
                                <div className={cn("h-2.5 w-2.5 rounded-full",
                                    status === 'completed' ? "bg-green-500" :
                                        status === 'error' ? "bg-red-500" :
                                            busy ? "bg-yellow-500 animate-pulse" : "bg-gray-500"
                                )} />
                                <span className="text-sm font-medium">
                                    {status === 'idle' && "Ready"}
                                    {status === 'login_needed' && "Login required"}
                                    {status === 'otp_needed' && "Enter OTP"}
                                    {status === 'logged_in' && "Logged in"}
                                    {status === 'exporting' && "Checking Oura..."}
                                    {status === 'waiting_export' && "Waiting for Oura"}
                                    {status === 'downloading' && "Downloading and importing..."}
                                    {status === 'initializing' && "Starting sync..."}
                                    {status === 'ready_to_download' && "Export ready"}
                                    {status === 'completed' && "Sync complete"}
                                    {status === 'error' && "Error occurred"}
                                </span>
                            </div>

                            {statusMessage && status !== 'otp_needed' && status !== 'error' && (
                                <p className="text-sm text-muted-foreground" role="status">{statusMessage}</p>
                            )}
                            <dl className="space-y-2 text-sm" aria-label="Data freshness">
                                <div><dt className="text-muted-foreground">Data available through</dt>
                                    <dd>{syncInfo?.data_through ? new Date(`${syncInfo.data_through}T12:00:00`).toLocaleDateString() : 'No imported data yet'}</dd></div>
                                <div><dt className="text-muted-foreground">Last checked</dt>
                                    <dd>{syncInfo?.last_checked_at ? new Date(syncInfo.last_checked_at).toLocaleString() : 'Not checked yet'}</dd></div>
                                <div><dt className="text-muted-foreground">Last imported</dt>
                                    <dd>{syncInfo?.last_imported_at ? new Date(syncInfo.last_imported_at).toLocaleString() : 'Not imported yet'}</dd></div>
                                {syncInfo?.export_pending && <div><dt className="text-muted-foreground">Export request</dt>
                                    <dd>{syncInfo.export_requested_at ? `Pending since ${new Date(syncInfo.export_requested_at).toLocaleString()}` : 'Pending with Oura'}</dd></div>}
                                {syncInfo?.next_export_check_at && <div><dt className="text-muted-foreground">Next export check</dt>
                                    <dd>{new Date(syncInfo.next_export_check_at).toLocaleString()}</dd></div>}
                            </dl>
                            <p className="text-xs text-muted-foreground">Checks resume while the app is running and your Mac is awake. Waiting for an export does not mean new data has arrived.</p>
                            <div className="grid grid-cols-1 gap-3">
                                {/* Login Flow */}
                                {!busy && status !== 'otp_needed' && (
                                    <div className="space-y-3 p-3 border rounded-lg">
                                        <Label>Login</Label>
                                        <Input
                                            placeholder="email@example.com"
                                            value={email}
                                            onChange={e => setEmail(e.target.value)}
                                        />
                                        <Button className="w-full" onClick={handleStartLogin} disabled={!email || busy}>
                                            {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                                            {status === 'login_needed' ? 'Sign in again' : 'Sign in / Check session'}
                                        </Button>
                                    </div>
                                )}

                                {status === 'otp_needed' && (
                                    <div className="space-y-3 p-3 border rounded-lg bg-secondary/10">
                                        <Alert>
                                            <AlertCircle className="h-4 w-4" />
                                            <AlertTitle>Email code required</AlertTitle>
                                            <AlertDescription>{statusMessage || 'Check your email. Sync is paused until you enter the code.'}</AlertDescription>
                                        </Alert>
                                        <Input
                                            aria-label="Email verification code"
                                            autoComplete="one-time-code"
                                            inputMode="numeric"
                                            placeholder="Email verification code"
                                            value={otp}
                                            onChange={e => setOtp(e.target.value)}
                                        />
                                        <Button className="w-full" onClick={handleSubmitOtp} disabled={!otp || busy}>
                                            {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                                            Verify code and continue
                                        </Button>
                                        <Button variant="outline" onClick={handleStartLogin} disabled={busy || !email}>
                                            Request a new code
                                        </Button>
                                    </div>
                                )}

                                {/* Actions */}
                                <div className="space-y-2">
                                    <Label>Data Sync</Label>
                                    <div className="grid grid-cols-2 gap-2">
                                        <Button
                                            variant="outline"
                                            onClick={handleRequestExport}
                                            disabled={busy || status === 'otp_needed' || status === 'login_needed'}
                                            className="h-auto py-3 flex flex-col gap-1"
                                        >
                                            <span className="font-semibold">{syncInfo?.export_pending ? 'Check export' : 'Request New'}</span>
                                            <span className="text-xs font-normal text-muted-foreground">{syncInfo?.export_pending ? 'Keep existing request' : 'Create if needed'}</span>
                                        </Button>
                                        <Button
                                            variant="outline"
                                            onClick={handleDownload}
                                            disabled={busy || status === 'otp_needed' || status === 'login_needed'}
                                            className="h-auto py-3 flex flex-col gap-1"
                                        >
                                            <span className="font-semibold">Download Latest</span>
                                            <span className="text-xs font-normal text-muted-foreground">Ingest Existing</span>
                                        </Button>
                                    </div>
                                </div>

                                {/* Manual Import */}
                                <div className="space-y-2">
                                    <Label>Manual Upload</Label>
                                    <div className="flex gap-2">
                                        <Input
                                            type="file"
                                            accept=".zip"
                                            onChange={handleFileUpload}
                                            disabled={busy}
                                            className="cursor-pointer"
                                        />
                                    </div>
                                    <p className="text-[10px] text-muted-foreground">
                                        Upload an Oura export ZIP file manually.
                                    </p>
                                </div>
                            </div>

                            {status === 'ready_to_download' && (
                                <Alert className="bg-blue-500/10 border-blue-500/20">
                                    <Download className="h-4 w-4 text-blue-500" />
                                    <AlertTitle>Export Ready</AlertTitle>
                                    <AlertDescription>
                                        Data is ready. Click "Download Latest" to ingest.
                                    </AlertDescription>
                                </Alert>
                            )}

                            {error && (
                                <Alert variant="destructive">
                                    <AlertCircle className="h-4 w-4" />
                                    <AlertTitle>Error</AlertTitle>
                                    <AlertDescription>{error}</AlertDescription>
                                </Alert>
                            )}
                        </div>

                        {/* Logs Console */}
                        <div className="space-y-2">
                            <Label>Activity Log</Label>
                            <div className="bg-black/50 rounded-md p-3 h-32 overflow-y-auto font-mono text-xs text-muted-foreground space-y-1">
                                {logs.length === 0 && <span className="opacity-50">No activity yet...</span>}
                                {logs.map((log, i) => (
                                    <div key={i}>{log}</div>
                                ))}
                            </div>
                        </div>

                        {/* Session Management (Bottom) */}
                        <div className="pt-4 border-t space-y-4">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Session</h3>
                            <Button
                                variant="destructive"
                                className="w-full"
                                onClick={handleClearSession}
                                disabled={busy}
                            >
                                Clear Login Session
                            </Button>
                        </div>
                    </>
                )}

                {activeTab === 'layout' && (
                    <div className="space-y-4">
                        <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Layout Actions</h3>

                        <div className="grid grid-cols-1 gap-3">
                            <Button variant="outline" onClick={() => {
                                api.getLayout()
                                    .then(data => {
                                        const layoutJson = JSON.stringify(data, null, 2);
                                        navigator.clipboard.writeText(layoutJson);
                                        addLog("Layout config copied to clipboard.");
                                    })
                                    .catch(err => {
                                        console.error("Failed to fetch layout", err);
                                    });
                            }}>
                                <Copy className="mr-2 h-4 w-4" />
                                Copy Layout to Clipboard
                            </Button>

                            <div className="space-y-2">
                                <Label>Import Layout</Label>
                                <textarea
                                    placeholder="Paste layout JSON here..."
                                    className="flex min-h-[150px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 font-mono text-[10px]"
                                    id="import-layout-area"
                                />
                                <Button
                                    variant="outline"
                                    className="w-full"
                                    onClick={async () => {
                                        const el = document.getElementById('import-layout-area') as HTMLTextAreaElement;
                                        if (!el || !el.value) return;

                                        try {
                                            const rawJson = JSON.parse(el.value);
                                            let payload = rawJson;

                                            // Handle case where export is wrapped in "dashboard" key
                                            if (rawJson.dashboard && rawJson.dashboard.dashboards) {
                                                payload = rawJson.dashboard;
                                            }

                                            // Validation
                                            if (!payload.dashboards && !payload.widgets) {
                                                alert("Invalid JSON: Must contain 'dashboards' or 'widgets' property.");
                                                return;
                                            }

                                            await api.saveLayout(payload);
                                            alert("Layout imported successfully! The page will reload.");
                                            window.location.reload();
                                            el.value = "";
                                        } catch (e: any) {
                                            alert("Import Failed: " + e.message);
                                        }
                                    }}
                                >
                                    <Upload className="mr-2 h-4 w-4" />
                                    Import Layout
                                </Button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div >
    );
}
