# Universal Task Sync — Semantic Configuration & Adoption Specification

## 1. Purpose

This document specifies the architecture, behavior, and guarantees for semantic discovery, configuration, and adoption in **universal-task-sync (UTS)**. The goal is to enable safe, explainable, user-controlled synchronization between heterogeneous task management systems (Taskwarrior, GitHub, Redmine, etc.) without silent semantic drift.

## 2. Design Principles

1. Semantics are explicit
2. No silent changes
3. Monotonic evolution
4. Project-local authority
5. Tool-scoped semantics

## 3. Core Concepts

### 3.1 Semantic Entity
A semantic entity represents a conceptual task attribute independent of any tool.

Examples: bug, enhancement, firstissue, verification, rtl

### 3.2 Semantic Role
Each semantic entity has exactly one global role: label, container, status, priority.

Roles never change globally.

### 3.3 CIF (Common Intermediate Form)
CIF is the normalized internal representation of tasks.

## 4. Configuration Model

### 4.1 Global Configuration
Defines semantic vocabulary and default mappings. Additive only.

### 4.2 Project Configuration
Overrides global defaults. Explicit None means intentionally ignored.

## 5. Discovery Model
Plugins discover raw tool entities which are classified semantically.

## 6. Change Detection
Detects new semantics and computes affected projects.

## 7. Proposal Model
Semantic proposals require explicit user decisions.

## 8. Interaction Model
Supports interactive ($EDITOR) and non-interactive modes.

## 9. Persistence Rules
Project config is the only memory of decisions.

## 10. Safety Guarantees
No silent changes or surprise writes.

## 11. GitHub-Specific Semantics
Projects v2 are primary containers. GraphQL preferred.

## 12. Evolution Model
Global config evolves additively; project config freezes intent.

## 13. Summary
Stable semantic mediation layer for task systems.
