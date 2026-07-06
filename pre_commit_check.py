"""
Pre-commit checks for template engine modifications.
READ-ONLY - does not modify any files.
"""

import re
import yaml
from pathlib import Path

# Exclusions for CHECK 2
INTERNAL_OBJECTS = {
    'deal_data', 'contact_data', 'examt3p_data', 'threads', 'enriched_lookups',
    'date_examen_vtc_data', 'session_data', 'uber_eligibility_data',
    'training_exam_consistency_data', 'cab_proposals', 'thread_memory',
    'conversation_state', 'crm_notes', 'primary_intent', 'secondary_intents',
    'detected_intent', 'intent_context', 'customer_message', 'ticket_subject',
    'section0_overrides', 'triage_full_result', 'cross_dept_data',
    'deal_notes', 'timeline_data', 'v3_response_mode', 'ticket_id', 'department'
}

def check_2_template_variable_whitelist():
    """CHECK 2: Detect variables in context_data but not in whitelist."""
    print("=" * 80)
    print("CHECK 2: Template Variable Whitelist (workflow -> engine)")
    print("=" * 80)

    # Extract context_data keys from workflow
    workflow_path = Path("src/workflows/doc_ticket_workflow.py")
    workflow_content = workflow_path.read_text(encoding='utf-8')

    # Pattern: context_data['key'] or context_data.update({
    context_keys = set()

    # Extract from direct assignments: context_data['key'] = ...
    for match in re.finditer(r"context_data\['([^']+)'\]", workflow_content):
        key = match.group(1)
        if key not in INTERNAL_OBJECTS:
            context_keys.add(key)

    # Extract from .update() calls
    update_blocks = re.findall(r"context_data\.update\(\{([^}]+)\}\)", workflow_content, re.DOTALL)
    for block in update_blocks:
        for match in re.finditer(r"['\"]([^'\"]+)['\"]:", block):
            key = match.group(1)
            if key not in INTERNAL_OBJECTS:
                context_keys.add(key)

    print(f"Found {len(context_keys)} context_data keys (excluding internal objects)")

    # Extract whitelist from _prepare_placeholder_data
    engine_path = Path("src/state_engine/template_engine.py")
    engine_content = engine_path.read_text(encoding='utf-8')

    # Find _prepare_placeholder_data method
    method_start = engine_content.find("def _prepare_placeholder_data")
    if method_start == -1:
        print("[X] ERROR: Could not find _prepare_placeholder_data method")
        return

    # Extract all keys from the method (improved pattern)
    # Look for 'key' in result dict AND result['key'] assignments AND context.get('key') references
    method_section = engine_content[method_start:method_start+80000]

    whitelist_keys = set()

    # Pattern 1: 'key': value in result dict
    for match in re.finditer(r"['\"]([a-z_][a-z0-9_]*)['\"]:\s*", method_section):
        whitelist_keys.add(match.group(1))

    # Pattern 2: result['key'] = ...
    for match in re.finditer(r"result\['([a-z_][a-z0-9_]*)'\]", method_section):
        whitelist_keys.add(match.group(1))

    # Pattern 3: Keys passed through helper methods that return dicts with **
    # These are merged into result via ** unpacking (e.g., **self._generate_report_flags())
    helper_pattern = r"\*\*self\._([a-z_]+)\(context\)"
    helper_methods = re.findall(helper_pattern, method_section)

    # For each helper, find what keys it returns
    for helper_name in helper_methods:
        helper_start = engine_content.find(f"def _{helper_name}(")
        if helper_start > -1:
            helper_section = engine_content[helper_start:helper_start+5000]
            for match in re.finditer(r"['\"]([a-z_][a-z0-9_]*)['\"]:\s*", helper_section):
                whitelist_keys.add(match.group(1))

    print(f"Found {len(whitelist_keys)} keys in whitelist")

    # Compare
    missing = context_keys - whitelist_keys

    if not missing:
        print("[OK] CHECK 2: OK - All context_data keys are in whitelist")
    else:
        print(f"[X] CHECK 2: {len(missing)} problem(s)")
        print("\nKeys in context_data but NOT in _prepare_placeholder_data():")
        for key in sorted(missing):
            print(f"  - {key}")

    print()

def check_5_section0_override_sync():
    """CHECK 5: Verify section0_overrides matches response_master.html Section 0."""
    print("=" * 80)
    print("CHECK 5: Section0 Override Sync")
    print("=" * 80)

    # Extract section0_overrides from template_engine.py
    engine_path = Path("src/state_engine/template_engine.py")
    engine_content = engine_path.read_text(encoding='utf-8')

    overrides_match = re.search(r"section0_overrides = \{([^}]+)\}", engine_content, re.DOTALL)
    if not overrides_match:
        print("[X] ERROR: Could not find section0_overrides dict")
        return

    overrides_text = overrides_match.group(1)
    section0_overrides = {}

    for match in re.finditer(r"'([^']+)':\s*\[([^\]]+)\]", overrides_text):
        intention_flag = match.group(1)
        flags_text = match.group(2)
        flags = [f.strip().strip("'\"") for f in flags_text.split(',')]
        section0_overrides[intention_flag] = flags

    print(f"Found {len(section0_overrides)} intention overrides in section0_overrides")

    # Extract Section 0 flags from response_master.html
    master_path = Path("states/templates/response_master.html")
    master_content = master_path.read_text(encoding='utf-8')

    # Find Section 0 (before Section 1)
    section0_start = master_content.find("SECTION 0:")
    section1_start = master_content.find("SECTION 1:")

    if section0_start == -1 or section1_start == -1:
        print("[X] ERROR: Could not find Section 0 or Section 1 markers")
        return

    section0_content = master_content[section0_start:section1_start]

    # Extract all {{#if xxx}} flags
    section0_flags = set()
    for match in re.finditer(r"\{\{#if ([a-z_]+)\}\}", section0_content):
        flag = match.group(1)
        section0_flags.add(flag)

    print(f"Found {len(section0_flags)} unique flags in Section 0 of response_master.html")

    # Verify each intention's flags exist in Section 0
    problems = []

    for intention, flags in section0_overrides.items():
        for flag in flags:
            if flag not in section0_flags:
                problems.append(f"  - {intention}: references '{flag}' but not in Section 0")

    # Verify inverse: resultat_*, report_*, credentials_*, uber_* flags in Section 0 are in overrides
    covered_flags = set()
    for flags in section0_overrides.values():
        covered_flags.update(flags)

    for flag in section0_flags:
        if flag.startswith(('resultat_', 'report_', 'credentials_', 'uber_')):
            if flag not in covered_flags:
                problems.append(f"  - Section 0 has '{flag}' but not listed in section0_overrides")

    if not problems:
        print("[OK] CHECK 5: OK - section0_overrides is in sync with response_master.html")
    else:
        print(f"[X] CHECK 5: {len(problems)} problem(s)")
        for problem in problems:
            print(problem)

    print()

def check_6_matrix_rule11_compliance():
    """CHECK 6: Verify Rule 11 compliance for protected flags."""
    print("=" * 80)
    print("CHECK 6: Matrix Rule 11 Compliance")
    print("=" * 80)

    PROTECTED_FLAGS = ['show_dates_section', 'show_sessions_section', 'show_statut_section', 'show_session_info']

    engine_path = Path("src/state_engine/template_engine.py")
    engine_content = engine_path.read_text(encoding='utf-8')
    lines = engine_content.split('\n')

    problems = []

    for flag in PROTECTED_FLAGS:
        # Find all assignments to this flag
        assignment_pattern = rf"result\['{flag}'\]\s*="

        for i, line in enumerate(lines, 1):
            if re.search(assignment_pattern, line):
                # Check if there's a guard within previous 5 lines
                guard_pattern = rf"if ['\"]?{flag}['\"]? (not )?in context:"
                has_guard = False

                for j in range(max(0, i-6), i):
                    if re.search(guard_pattern, lines[j]):
                        has_guard = True
                        break

                # Check if it's in the initial result = { ... } dict
                is_initial_dict = 'result = {' in '\n'.join(lines[max(0, i-10):i])

                # Check if it reads from context
                reads_context = 'context.get' in line or 'context[' in line

                if not (has_guard or is_initial_dict or reads_context):
                    problems.append(f"  - Line {i}: {flag} assigned without guard: {line.strip()}")

    if not problems:
        print("[OK] CHECK 6: OK - All protected flags respect Rule 11")
    else:
        print(f"[!]  CHECK 6: {len(problems)} potential issue(s)")
        print("\nNote: First initialization in result = {...} is OK if it uses context.get()")
        for problem in problems:
            print(problem)

    print()

def check_8_matrix_context_flags_to_whitelist():
    """CHECK 8: Verify matrix context_flags arrive at templates."""
    print("=" * 80)
    print("CHECK 8: Matrix Context Flags -> Whitelist")
    print("=" * 80)

    # Load matrix - need to handle entries without intention definitions
    matrix_path = Path("states/state_intention_matrix.yaml")
    with open(matrix_path, encoding='utf-8') as f:
        matrix_content = f.read()

    matrix_data = yaml.safe_load(matrix_content)

    # Extract all context_flags from matrix entries (not from intentions section)
    all_context_flags = set()

    for key, value in matrix_data.items():
        # Skip the top-level 'intentions' key
        if key == 'intentions':
            continue
        if isinstance(value, dict) and 'context_flags' in value:
            flags = value['context_flags']
            if isinstance(flags, dict):
                all_context_flags.update(flags.keys())

    print(f"Found {len(all_context_flags)} unique context_flags in matrix")

    # Split into intention_* and others
    intention_flags = {f for f in all_context_flags if f.startswith('intention_')}
    other_flags = all_context_flags - intention_flags

    print(f"  - {len(intention_flags)} intention_* flags (skipping)")
    print(f"  - {len(other_flags)} other flags to verify")

    # Check if they're in whitelist
    engine_path = Path("src/state_engine/template_engine.py")
    engine_content = engine_path.read_text(encoding='utf-8')

    method_start = engine_content.find("def _prepare_placeholder_data")
    if method_start == -1:
        print("[X] ERROR: Could not find _prepare_placeholder_data method")
        return

    method_content = engine_content[method_start:method_start+50000]

    problems = []
    for flag in sorted(other_flags):
        # Check if it appears in result dict or assignments
        if f"'{flag}'" not in method_content and f'"{flag}"' not in method_content:
            # Special case: show_* flags might be assigned dynamically
            if flag.startswith('show_'):
                if f"result['{flag}']" not in method_content:
                    problems.append(f"  - {flag}: NOT found in whitelist (show_* flag)")
            else:
                problems.append(f"  - {flag}: NOT found in whitelist")

    if not problems:
        print("[OK] CHECK 8: OK - All matrix context_flags are exposed to templates")
    else:
        print(f"[X] CHECK 8: {len(problems)} problem(s)")
        for problem in problems:
            print(problem)

    print()

if __name__ == '__main__':
    check_2_template_variable_whitelist()
    check_5_section0_override_sync()
    check_6_matrix_rule11_compliance()
    check_8_matrix_context_flags_to_whitelist()

    print("=" * 80)
    print("Pre-commit checks complete")
    print("=" * 80)
