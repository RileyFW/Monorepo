'use server';
import * as k8s from '@kubernetes/client-node';
import { auth } from '../auth';
import { getDocumentFromId } from './mongodb_funcs';

export async function getK8sLogs(expId: string) {
    // Check if the user is authenticated
    const session = await auth();
    if (!session) {
        throw new Error('User not authenticated');
    }
    // Check if the user has permission to access the experiment
    const exp = await getDocumentFromId(expId);
    if (!exp) {
        throw new Error('Experiment not found');
    }
    const creatorId = (exp as { _id: string; creator?: string }).creator;
    if (creatorId !== session?.user?.id) {
        throw new Error('User does not have permission to access this experiment');
    }
    const kc = new k8s.KubeConfig();
    kc.loadFromCluster();
    const k8sApi = kc.makeApiClient(k8s.CoreV1Api);

    const jobName = `runner-${expId}`;
    const result = k8sApi.listNamespacedPod({ namespace: 'default' }).then(async (res) => {
        // An experiment now runs as an Indexed Job with one pod per shard
        // (plus a one-shot runner-<expId>-finalize pod), so gather and label the
        // logs from every matching pod rather than just the first one.
        const pods = res.items
            .filter((pod) => pod.metadata?.name?.startsWith(jobName))
            .sort((a, b) => (a.metadata?.name ?? '').localeCompare(b.metadata?.name ?? ''));
        if (pods.length === 0) {
            throw new Error(`Pod not found for experiment ID: ${expId}`);
        }

        const sections = await Promise.all(pods.map(async (pod) => {
            const podName = pod.metadata?.name as string;
            // A shard's index is exposed on the pod via the standard Job annotation.
            const shardIndex = pod.metadata?.annotations?.['batch.kubernetes.io/job-completion-index'];
            const label = podName.endsWith('finalize') || podName.includes('-finalize-')
                ? 'Finalize'
                : shardIndex !== undefined ? `Shard ${shardIndex}` : podName;
            try {
                const log = await k8sApi.readNamespacedPodLog({ name: podName, namespace: 'default' });
                return `===== ${label} (${podName}) =====\n${log}`;
            } catch (error) {
                return `===== ${label} (${podName}) =====\n[log unavailable: ${String(error)}]`;
            }
        }));

        return sections.join('\n\n');
    });

    return result;
}

export async function triggerRedeploy() {
    const session = await auth();
    if (!session) {
        throw new Error('User not authenticated');
    }
    if (!session?.user?.role || session?.user?.role !== 'admin') {
        throw new Error('User does not have permission to trigger redeploy');
    }

    const kc = new k8s.KubeConfig();
    kc.loadFromDefault();
    const appsApi = kc.makeApiClient(k8s.AppsV1Api);

    const deployments = [
        { name: 'glados-frontend', namespace: 'default' },
        { name: 'glados-backend', namespace: 'default' },
    ];

    const patchBody = {
        spec: {
            template: {
                metadata: {
                    annotations: {
                        'kubectl.kubernetes.io/restartedAt': new Date().toISOString(),
                    },
                },
            },
        },
    };

    // First cordon glados-w0 node
    const nodeName = 'glados-w0';
    const k8sApi = kc.makeApiClient(k8s.CoreV1Api);
    const cordonPatch = {spec: { unschedulable: true }};
    const uncordonPatch = {spec: { unschedulable: false }};
    // Check if the node exists
    const nodeList = await k8sApi.listNode();
    const nodeExists = nodeList.items.some((node) => node.metadata?.name === nodeName);
    if (nodeExists) {
        try {
            await k8sApi.patchNode({
                name: nodeName,
                body: cordonPatch,
                pretty: 'true', // Optional pretty-printing
            }, k8s.setHeaderOptions('Content-Type', k8s.PatchStrategy.MergePatch));
        } catch (error) {
            console.error(`Failed to cordon node ${nodeName}:`, error);
        }
    }


    // Patch each deployment
    for (const { name, namespace } of deployments) {
        await appsApi.patchNamespacedDeployment({
            name,
            namespace,
            body: patchBody,
            pretty: 'true', // Optional pretty-printing
        }, k8s.setHeaderOptions('Content-Type', k8s.PatchStrategy.MergePatch));
    }

    if (nodeExists) {
        // Uncordon the node after patching
        await k8sApi.patchNode({
            name: nodeName,
            body: uncordonPatch,
            pretty: 'true', // Optional pretty-printing
        }, k8s.setHeaderOptions('Content-Type', k8s.PatchStrategy.MergePatch));
    }
}

