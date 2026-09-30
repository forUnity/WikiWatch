use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};

type EntityId = i64;
type PropertyId = i64;
type TypeId = i64;
type Count = i32;
type RelationId = i64;

const P31: PropertyId = 31;
const P279: PropertyId = 279;

const Q_RELATION_INSTANCE_OF: RelationId = 21503252;
const Q_RELATION_SUBCLASS_OF: RelationId = 21514624;
const Q_RELATION_INSTANCE_OR_SUBCLASS_OF: RelationId = 30208840;

#[derive(Debug, Clone)]
struct Rule {
    required_types: Vec<TypeId>,
    relation: RelationId,
    exceptions: FxHashSet<EntityId>,
}

#[pyclass]
pub struct SubjectConstraintCore {
    // entity_id -> direct P31 types
    entity_to_types: FxHashMap<EntityId, FxHashSet<TypeId>>,
    // type_id -> entities with direct P31=type_id
    type_to_entities: FxHashMap<TypeId, FxHashSet<EntityId>>,
    // child -> direct parents
    parents_of: FxHashMap<TypeId, FxHashSet<TypeId>>,
    // parent -> direct children
    children_of: FxHashMap<TypeId, FxHashSet<TypeId>>,
    // entity_id -> {property_id: statement_count}
    entity_props: FxHashMap<EntityId, FxHashMap<PropertyId, Count>>,
    // property -> list of rules
    rules_by_property: FxHashMap<PropertyId, Vec<Rule>>,
    // class_id -> transitive descendants
    descendants_cache: FxHashMap<TypeId, FxHashSet<TypeId>>,
}

#[pymethods]
impl SubjectConstraintCore {
    #[new]
    pub fn new(
        property_ids: Vec<i64>,
        rules_by_property: Vec<(i64, Vec<(Vec<i64>, i64, Vec<i64>)>)>,
    ) -> PyResult<Self> {
        let prop_set: FxHashSet<PropertyId> = property_ids.into_iter().collect();

        let mut compiled_rules_by_property: FxHashMap<PropertyId, Vec<Rule>> = FxHashMap::default();

        for (prop, rules) in rules_by_property {
            if !prop_set.contains(&prop) {
                continue;
            }

            let mut compiled_rules = Vec::with_capacity(rules.len());
            for (required_types, relation, exceptions) in rules {
                compiled_rules.push(Rule {
                    required_types,
                    relation,
                    exceptions: exceptions.into_iter().collect(),
                });
            }

            compiled_rules_by_property.insert(prop, compiled_rules);
        }

        Ok(Self {
            entity_to_types: FxHashMap::default(),
            type_to_entities: FxHashMap::default(),
            parents_of: FxHashMap::default(),
            children_of: FxHashMap::default(),
            entity_props: FxHashMap::default(),
            rules_by_property: compiled_rules_by_property,
            descendants_cache: FxHashMap::default(),
        })
    }

    pub fn load_entity_to_types(&mut self, rows: Vec<(i64, i64)>) {
        self.entity_to_types.clear();
        self.type_to_entities.clear();

        for (eid, tid) in rows {
            self.entity_to_types.entry(eid).or_default().insert(tid);
            self.type_to_entities.entry(tid).or_default().insert(eid);
        }
    }

    pub fn load_parents_of(&mut self, rows: Vec<(i64, i64)>) {
        self.parents_of.clear();
        self.children_of.clear();
        self.descendants_cache.clear();

        for (child, parent) in rows {
            self.parents_of.entry(child).or_default().insert(parent);
            self.children_of.entry(parent).or_default().insert(child);
        }
    }

    pub fn load_entity_props(&mut self, rows: Vec<(i64, i64, i32)>) {
        self.entity_props.clear();

        for (eid, pid, count) in rows {
            if count > 0 {
                self.entity_props.entry(eid).or_default().insert(pid, count);
            }
        }
    }

    pub fn export_entity_to_types(&self) -> Vec<(i64, i64)> {
        let mut out = Vec::new();
        for (eid, types) in &self.entity_to_types {
            for tid in types {
                out.push((*eid, *tid));
            }
        }
        out
    }

    pub fn export_parents_of(&self) -> Vec<(i64, i64)> {
        let mut out = Vec::new();
        for (child, parents) in &self.parents_of {
            for parent in parents {
                out.push((*child, *parent));
            }
        }
        out
    }

    pub fn export_entity_props(&self) -> Vec<(i64, i64, i32)> {
        let mut out = Vec::new();
        for (eid, props) in &self.entity_props {
            for (pid, count) in props {
                out.push((*eid, *pid, *count));
            }
        }
        out
    }

    pub fn process_batch(
        &mut self,
        entity_ids: Vec<i64>,
        property_ids: Vec<i64>,
        actions: Vec<i8>,   // 1=create, -1=delete, 0=update/other
        old_qids: Vec<i64>, // 0 => no qid
        new_qids: Vec<i64>, // 0 => no qid
    ) -> PyResult<Vec<(i64, i64)>> {
        let n = entity_ids.len();
        if property_ids.len() != n || actions.len() != n || old_qids.len() != n || new_qids.len() != n {
            return Err(PyValueError::new_err("all input arrays must have same length"));
        }

        let mut local_satisfies_cache: FxHashMap<EntityId, FxHashMap<PropertyId, bool>> =
            FxHashMap::default();
        let mut local_ancestor_cache: FxHashMap<TypeId, FxHashSet<TypeId>> = FxHashMap::default();

        let mut prop_net_changes: FxHashMap<(EntityId, PropertyId), i32> = FxHashMap::default();
        let mut p31_net_changes: FxHashMap<(EntityId, TypeId), i32> = FxHashMap::default();
        let mut p279_net_changes: FxHashMap<(TypeId, TypeId), i32> = FxHashMap::default();

        // 1) Collect all batch changes
        for i in 0..n {
            let prop = property_ids[i];
            let entity_id = entity_ids[i];
            let action = actions[i];

            if prop == P31 {
                let old_t = old_qids[i];
                let new_t = new_qids[i];

                if action != 1 && old_t > 0 {
                    *p31_net_changes.entry((entity_id, old_t)).or_insert(0) -= 1;
                }
                if action != -1 && new_t > 0 {
                    *p31_net_changes.entry((entity_id, new_t)).or_insert(0) += 1;
                }
                continue;
            }

            if prop == P279 {
                let old_t = old_qids[i];
                let new_t = new_qids[i];

                if action != 1 && old_t > 0 {
                    *p279_net_changes.entry((entity_id, old_t)).or_insert(0) -= 1;
                }
                if action != -1 && new_t > 0 {
                    *p279_net_changes.entry((entity_id, new_t)).or_insert(0) += 1;
                }
                continue;
            }

            if !self.rules_by_property.contains_key(&prop) {
                continue;
            }

            if action == 1 {
                *prop_net_changes.entry((entity_id, prop)).or_insert(0) += 1;
            } else if action == -1 {
                *prop_net_changes.entry((entity_id, prop)).or_insert(0) -= 1;
            }
        }

        // 2) Determine potentially affected entities/properties in the OLD state
        let p31_entities: FxHashSet<EntityId> = p31_net_changes
            .iter()
            .filter_map(|((e, _), net)| if *net != 0 { Some(*e) } else { None })
            .collect();

        let changed_classes: FxHashSet<TypeId> = p279_net_changes
            .iter()
            .filter_map(|((child, _), net)| if *net != 0 { Some(*child) } else { None })
            .collect();

        let mut affected_props_by_entity: FxHashMap<EntityId, FxHashSet<PropertyId>> =
            FxHashMap::default();
        for ((e, prop), delta) in &prop_net_changes {
            if *delta != 0 {
                affected_props_by_entity.entry(*e).or_default().insert(*prop);
            }
        }

        let mut affected_entities_before: FxHashSet<EntityId> =
            affected_props_by_entity.keys().copied().collect();
        affected_entities_before.extend(p31_entities.iter().copied());
        if !changed_classes.is_empty() {
            let extra = self.affected_entities_from_changed_classes(&changed_classes);
            affected_entities_before.extend(extra);
        }

        for e in &affected_entities_before {
            if let Some(props) = self.entity_props.get(e) {
                let entry = affected_props_by_entity.entry(*e).or_default();
                entry.extend(props.keys().copied());
            }
        }

        // 3) Snapshot violations BEFORE
        let old_violations = self.snapshot_violations(
            &affected_props_by_entity,
            &mut local_satisfies_cache,
            &mut local_ancestor_cache,
        );

        // 4) Apply all changes to reach NEW state
        self.apply_property_net_changes(&prop_net_changes);
        self.apply_p31_batch(&p31_net_changes);
        self.apply_p279_batch(&p279_net_changes);

        if !changed_classes.is_empty() {
            self.descendants_cache.clear();
            local_ancestor_cache.clear();
        }
        local_satisfies_cache.clear();

        // 5) Determine potentially affected entities/properties in the NEW state
        let mut affected_entities_after: FxHashSet<EntityId> =
            affected_props_by_entity.keys().copied().collect();
        affected_entities_after.extend(p31_entities.iter().copied());
        if !changed_classes.is_empty() {
            let extra = self.affected_entities_from_changed_classes(&changed_classes);
            affected_entities_after.extend(extra);
        }

        let mut affected_entities = affected_entities_before;
        affected_entities.extend(affected_entities_after);

        for e in &affected_entities {
            if let Some(props) = self.entity_props.get(e) {
                let entry = affected_props_by_entity.entry(*e).or_default();
                entry.extend(props.keys().copied());
            }
        }

        // 6) Recompute AFTER and subtract
        let mut diff_by_prop: FxHashMap<PropertyId, i64> = FxHashMap::default();

        for e in affected_entities {
            let Some(props) = affected_props_by_entity.get(&e) else {
                continue;
            };

            let old_for_entity = old_violations.get(&e);

            for prop in props {
                let new_viol = self.entity_violation_count_for_prop(
                    e,
                    *prop,
                    &mut local_satisfies_cache,
                    &mut local_ancestor_cache,
                );
                let old_viol = old_for_entity
                    .and_then(|m| m.get(prop))
                    .copied()
                    .unwrap_or(0);

                let delta = new_viol - old_viol;
                if delta != 0 {
                    *diff_by_prop.entry(*prop).or_insert(0) += delta as i64;
                }
            }
        }

        let mut out: Vec<(i64, i64)> = diff_by_prop.into_iter().collect();
        out.sort_unstable_by_key(|(prop, _)| *prop);

        Ok(out)
    }
}

impl SubjectConstraintCore {
    fn get_ancestors_cached<'a>(
        parents_of: &FxHashMap<TypeId, FxHashSet<TypeId>>,
        child: TypeId,
        local_ancestor_cache: &'a mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> &'a FxHashSet<TypeId> {
        if !local_ancestor_cache.contains_key(&child) {
            let mut ancestors: FxHashSet<TypeId> = FxHashSet::default();
            let mut stack: Vec<TypeId> = parents_of
                .get(&child)
                .map(|s| s.iter().copied().collect())
                .unwrap_or_default();

            while let Some(cur) = stack.pop() {
                if !ancestors.insert(cur) {
                    continue;
                }
                if let Some(parents) = parents_of.get(&cur) {
                    stack.extend(parents.iter().copied());
                }
            }

            local_ancestor_cache.insert(child, ancestors);
        }

        local_ancestor_cache.get(&child).unwrap()
    }

    fn get_descendants_cached(&mut self, class_id: TypeId) -> &FxHashSet<TypeId> {
        if !self.descendants_cache.contains_key(&class_id) {
            let mut descendants: FxHashSet<TypeId> = FxHashSet::default();
            let mut stack: Vec<TypeId> = self
                .children_of
                .get(&class_id)
                .map(|s| s.iter().copied().collect())
                .unwrap_or_default();

            while let Some(cur) = stack.pop() {
                if !descendants.insert(cur) {
                    continue;
                }
                if let Some(children) = self.children_of.get(&cur) {
                    stack.extend(children.iter().copied());
                }
            }

            self.descendants_cache.insert(class_id, descendants);
        }

        self.descendants_cache.get(&class_id).unwrap()
    }

    fn entity_matches_rule(
        &self,
        e: EntityId,
        rule: &Rule,
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> bool {
        if rule.exceptions.contains(&e) {
            return true;
        }

        if rule.required_types.is_empty() {
            return true;
        }

        let instance_match =
            self.entity_has_required_type(e, &rule.required_types, local_ancestor_cache);

        if rule.relation == Q_RELATION_INSTANCE_OF {
            return instance_match;
        }

        let subclass_match = self.entity_is_subclass_of_required_type(
            e,
            &rule.required_types,
            local_ancestor_cache,
        );

        if rule.relation == Q_RELATION_SUBCLASS_OF {
            return subclass_match;
        }

        if rule.relation == Q_RELATION_INSTANCE_OR_SUBCLASS_OF {
            return instance_match || subclass_match;
        }

        instance_match
    }

    fn entity_satisfies_prop(
        &self,
        e: EntityId,
        prop: PropertyId,
        local_satisfies_cache: &mut FxHashMap<EntityId, FxHashMap<PropertyId, bool>>,
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> bool {
        if let Some(entity_cache) = local_satisfies_cache.get(&e) {
            if let Some(cached) = entity_cache.get(&prop) {
                return *cached;
            }
        }

        let satisfied = match self.rules_by_property.get(&prop) {
            None => true,
            Some(rules) => rules
                .iter()
                .all(|rule| self.entity_matches_rule(e, rule, local_ancestor_cache)),
        };

        local_satisfies_cache
            .entry(e)
            .or_default()
            .insert(prop, satisfied);

        satisfied
    }

    fn entity_has_required_type(
        &self,
        e: EntityId,
        required: &[TypeId],
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> bool {
        if required.is_empty() {
            return true;
        }

        let Some(types) = self.entity_to_types.get(&e) else {
            return false;
        };

        for req in required {
            if types.contains(req) {
                return true;
            }
        }

        for t in types {
            let ancestors = Self::get_ancestors_cached(&self.parents_of, *t, local_ancestor_cache);
            if required.iter().any(|req| ancestors.contains(req)) {
                return true;
            }
        }

        false
    }

    fn entity_is_subclass_of_required_type(
        &self,
        e: EntityId,
        required: &[TypeId],
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> bool {
        if required.contains(&e) {
            return true;
        }

        let ancestors = Self::get_ancestors_cached(&self.parents_of, e, local_ancestor_cache);
        required.iter().any(|req| ancestors.contains(req))
    }

    fn entity_violation_count_for_prop(
        &self,
        e: EntityId,
        prop: PropertyId,
        local_satisfies_cache: &mut FxHashMap<EntityId, FxHashMap<PropertyId, bool>>,
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> i32 {
        let stmt_count = self
            .entity_props
            .get(&e)
            .and_then(|m| m.get(&prop))
            .copied()
            .unwrap_or(0);

        if stmt_count == 0 {
            return 0;
        }

        if self.entity_satisfies_prop(e, prop, local_satisfies_cache, local_ancestor_cache) {
            0
        } else {
            stmt_count
        }
    }

    fn affected_entities_from_changed_classes(
        &mut self,
        class_ids: &FxHashSet<TypeId>,
    ) -> FxHashSet<EntityId> {
        let mut affected_classes: FxHashSet<TypeId> = FxHashSet::default();
        let mut stack: Vec<TypeId> = class_ids.iter().copied().collect();

        while let Some(cur) = stack.pop() {
            if !affected_classes.insert(cur) {
                continue;
            }

            if let Some(cached_descendants) = self.descendants_cache.get(&cur) {
                affected_classes.extend(cached_descendants.iter().copied());
                continue;
            }

            if let Some(children) = self.children_of.get(&cur) {
                stack.extend(children.iter().copied());
            }
        }

        for class_id in class_ids {
            let _ = self.get_descendants_cached(*class_id);
        }

        let mut affected: FxHashSet<EntityId> = affected_classes.iter().copied().collect();
        for c in &affected_classes {
            if let Some(entities) = self.type_to_entities.get(c) {
                affected.extend(entities.iter().copied());
            }
        }

        affected
    }

    fn snapshot_violations(
        &self,
        affected_props_by_entity: &FxHashMap<EntityId, FxHashSet<PropertyId>>,
        local_satisfies_cache: &mut FxHashMap<EntityId, FxHashMap<PropertyId, bool>>,
        local_ancestor_cache: &mut FxHashMap<TypeId, FxHashSet<TypeId>>,
    ) -> FxHashMap<EntityId, FxHashMap<PropertyId, i32>> {
        let mut snapshots: FxHashMap<EntityId, FxHashMap<PropertyId, i32>> = FxHashMap::default();

        for (e, props) in affected_props_by_entity {
            if props.is_empty() {
                continue;
            }

            let mut inner: FxHashMap<PropertyId, i32> = FxHashMap::default();
            for p in props {
                inner.insert(
                    *p,
                    self.entity_violation_count_for_prop(
                        *e,
                        *p,
                        local_satisfies_cache,
                        local_ancestor_cache,
                    ),
                );
            }
            snapshots.insert(*e, inner);
        }

        snapshots
    }

    fn apply_property_net_changes(
        &mut self,
        prop_net_changes: &FxHashMap<(EntityId, PropertyId), i32>,
    ) {
        for ((e, prop), delta) in prop_net_changes {
            if *delta == 0 {
                continue;
            }

            let current_count = self
                .entity_props
                .get(e)
                .and_then(|m| m.get(prop))
                .copied()
                .unwrap_or(0);

            let mut new_count = current_count + *delta;
            if new_count < 0 {
                new_count = 0;
            }

            if new_count > 0 {
                self.entity_props.entry(*e).or_default().insert(*prop, new_count);
            } else {
                let remove_entity = if let Some(e_props) = self.entity_props.get_mut(e) {
                    e_props.remove(prop);
                    e_props.is_empty()
                } else {
                    false
                };
                if remove_entity {
                    self.entity_props.remove(e);
                }
            }
        }
    }

    fn apply_p31_batch(&mut self, p31_net_changes: &FxHashMap<(EntityId, TypeId), i32>) {
        for ((e, t), net) in p31_net_changes {
            if *net == 0 {
                continue;
            }

            let base_present = self
                .entity_to_types
                .get(e)
                .map(|s| s.contains(t))
                .unwrap_or(false) as i32;

            let final_present = if base_present + *net > 0 { 1 } else { 0 };

            if final_present == base_present {
                continue;
            }

            if final_present == 1 {
                self.entity_to_types.entry(*e).or_default().insert(*t);
                self.type_to_entities.entry(*t).or_default().insert(*e);
            } else {
                let remove_entity = if let Some(e_types) = self.entity_to_types.get_mut(e) {
                    e_types.remove(t);
                    e_types.is_empty()
                } else {
                    false
                };
                if remove_entity {
                    self.entity_to_types.remove(e);
                }

                let remove_type = if let Some(rev) = self.type_to_entities.get_mut(t) {
                    rev.remove(e);
                    rev.is_empty()
                } else {
                    false
                };
                if remove_type {
                    self.type_to_entities.remove(t);
                }
            }
        }
    }

    fn apply_p279_batch(&mut self, p279_net_changes: &FxHashMap<(TypeId, TypeId), i32>) {
        for ((child, parent), net) in p279_net_changes {
            if *net == 0 {
                continue;
            }

            let base_present = self
                .parents_of
                .get(child)
                .map(|s| s.contains(parent))
                .unwrap_or(false) as i32;

            let final_present = if base_present + *net > 0 { 1 } else { 0 };

            if final_present == base_present {
                continue;
            }

            if final_present == 1 {
                self.parents_of.entry(*child).or_default().insert(*parent);
                self.children_of.entry(*parent).or_default().insert(*child);
            } else {
                let remove_child = if let Some(parents) = self.parents_of.get_mut(child) {
                    parents.remove(parent);
                    parents.is_empty()
                } else {
                    false
                };
                if remove_child {
                    self.parents_of.remove(child);
                }

                let remove_parent = if let Some(children) = self.children_of.get_mut(parent) {
                    children.remove(child);
                    children.is_empty()
                } else {
                    false
                };
                if remove_parent {
                    self.children_of.remove(parent);
                }
            }
        }
    }
}

#[pymodule]
fn subject_constraint_core(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SubjectConstraintCore>()?;
    Ok(())
}