"""Execution protocol types shared by in-process and managed process adapters."""

from agent.base import AgentAdapter
from ma_runtime.models import (AdapterEvent, AgentDescriptor, CancelReceipt, CheckpointProposal,
                               ExecutionRequest, ProbeResult, TaskResult, WaitCondition)

__all__ = ["AgentAdapter", "AdapterEvent", "AgentDescriptor", "CancelReceipt", "CheckpointProposal",
           "ExecutionRequest", "ProbeResult", "TaskResult", "WaitCondition"]
