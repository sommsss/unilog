from typing import Any, Dict, List

import pandas as pd

from src.config import (
    AUTO_APPROVE_SCORE,
    CONFLICT_PENALTY,
    MIN_COMPLETENESS,
    MIN_CONFIDENCE,
    REVIEW_SCORE,
)
from src.logging_setup import get_logger

log = get_logger("validator")


class ValidationEngine:
    def __init__(self):
        # Weights for the final Quality Score (Total 100 points)
        self.weights = {
            'completeness': 40,  # 40 points if attributes are found (even partial)
            'confidence': 40,    # 40 points if facts are High/Medium confidence
            'normalization': 20  # 20 points if facts normalized successfully
        }
        
        # THRESHOLD SETTINGS (tuned in src/config.py)
        self.min_completeness = MIN_COMPLETENESS  # share of target attributes that must be found
        self.min_confidence = MIN_CONFIDENCE      # share of facts that must be High/Medium
        self.auto_approve_score = AUTO_APPROVE_SCORE  # >= this -> AUTO_APPROVED (green)
        self.review_score = REVIEW_SCORE              # >= this -> HUMAN_REVIEW (yellow), else FAILED (red)
        self.conflict_penalty = CONFLICT_PENALTY      # points deducted per conflicting attribute

    def route(self, score: float) -> str:
        """Traffic-light routing: green publishes, yellow queues for review, red fails."""
        if score >= self.auto_approve_score:
            return 'AUTO_APPROVED'
        if score >= self.review_score:
            return 'HUMAN_REVIEW'
        return 'FAILED'

    def detect_conflicts(self, facts: List[Dict[str, Any]]) -> List[str]:
        """Checks if there are multiple different normalized values for the same attribute."""
        value_map = {}
        conflicts = []
        
        for fact in facts:
            attr = fact.get('attribute_name')
            val = fact.get('normalized_value')
            
            if attr not in value_map:
                value_map[attr] = set()
            if val and val != 'null' and val != '':
                value_map[attr].add(val)
                
        for attr, unique_vals in value_map.items():
            if len(unique_vals) > 1:
                conflicts.append(attr)
                
        return conflicts

    def calculate_metrics(self, facts: List[Dict[str, Any]], target_attributes: List[str]) -> Dict[str, Any]:
        """Calculates completeness, confidence, and conflict metrics for a single product."""
        
        # Handle edge cases
        if not target_attributes or len(target_attributes) == 0:
            target_attributes = ['Power', 'Voltage', 'Weight', 'Dimensions']  # Fallback defaults
        
        # No facts extracted - fail
        if not facts or len(facts) == 0:
            return {
                'quality_score': 0,
                'status': 'FAILED',
                'review_reasons': 'No facts extracted from document',
                'completeness_pct': 0.0,
                'confidence_ratio': 0.0,
                'norm_success_pct': 0.0,
                'conflicts_detected': 'None',
                'fact_count': 0
            }
        
        # 1. COMPLETENESS: What percentage of target attributes were found?
        found_attrs = set()
        for fact in facts:
            attr_name = fact.get('attribute_name', '').strip()
            normalized_val = fact.get('normalized_value', '')
            
            # Count as found if it has a normalized value
            if normalized_val and normalized_val != 'null' and normalized_val != '':
                found_attrs.add(attr_name)
        
        completeness_ratio = len(found_attrs) / len(target_attributes) if target_attributes else 0
        
        # 2. CONFIDENCE: What % of facts have High or Medium confidence?
        total_facts = len(facts)
        high_med_count = 0
        for fact in facts:
            conf = str(fact.get('confidence', '')).lower().strip()
            if conf in ['high', 'medium']:
                high_med_count += 1
        
        confidence_ratio = high_med_count / total_facts if total_facts > 0 else 0
        
        # 3. NORMALIZATION SUCCESS: What % of facts normalized without errors?
        norm_success_count = 0
        for fact in facts:
            norm_flag = fact.get('normalization_flag', '').upper()
            if norm_flag == 'SUCCESS':
                norm_success_count += 1
        
        norm_ratio = norm_success_count / total_facts if total_facts > 0 else 0
        
        # 4. CONFLICT DETECTION
        conflicts = self.detect_conflicts(facts)
        
        # CALCULATE BASE SCORE (0-100)
        score = (
            (completeness_ratio * self.weights['completeness']) +
            (confidence_ratio * self.weights['confidence']) +
            (norm_ratio * self.weights['normalization'])
        )
        
        # Apply conflict penalty (per conflicting attribute)
        score -= self.conflict_penalty * len(conflicts)

        score = max(0, min(100, round(score, 1)))

        # DETERMINE ROUTING STATUS (green / yellow / red)
        status = self.route(score)
        reasons = []
        
        # Provide reasons for manual review
        if completeness_ratio < self.min_completeness:
            reasons.append(f'Low completeness ({completeness_ratio*100:.0f}% < {self.min_completeness*100:.0f}%)')
        
        if confidence_ratio < self.min_confidence:
            reasons.append(f'Low confidence ({confidence_ratio*100:.0f}% < {self.min_confidence*100:.0f}%)')
        
        if conflicts:
            reasons.append(f'Conflicts: {", ".join(conflicts)}')
        
        if score < self.auto_approve_score:
            reasons.append(f'Quality score {score} below auto-approve threshold ({self.auto_approve_score})')
        
        return {
            'quality_score': score,
            'status': status,
            'review_reasons': ' | '.join(reasons) if reasons else 'Meets all thresholds',
            'completeness_pct': round(completeness_ratio * 100, 1),
            'conflicts_detected': ', '.join(conflicts) if conflicts else 'None',
            'fact_count': total_facts,
            'confidence_ratio': round(confidence_ratio * 100, 1),
            'norm_success_pct': round(norm_ratio * 100, 1)
        }

    def process_catalog_validation(self, classified_df: pd.DataFrame, normalized_facts_df: pd.DataFrame) -> pd.DataFrame:
        """Processes the entire catalog and generates a validation dashboard matrix."""
        validation_reports = []
        
        # Group facts by product
        if len(normalized_facts_df) > 0:
            facts_by_product = normalized_facts_df.groupby('product_id')
        else:
            facts_by_product = None
        
        for _, row in classified_df.iterrows():
            product_id = row.get('Mfg_Part_Num')
            target_attrs = row.get('Target_Attributes', [])
            
            # Ensure target_attrs is a list
            if not isinstance(target_attrs, list):
                target_attrs = []
            
            # Get facts for this product
            if facts_by_product is not None and product_id in facts_by_product.groups:
                product_facts = facts_by_product.get_group(product_id).to_dict('records')
            else:
                product_facts = []
            
            # Calculate metrics
            metrics = self.calculate_metrics(product_facts, target_attrs)
            
            # Build report
            report = {
                'product_id': product_id,
                'category': row.get('Assigned_Category', 'Unknown'),
                'quality_score': metrics.get('quality_score'),
                'status': metrics.get('status'),
                'review_reasons': metrics.get('review_reasons'),
                'completeness_pct': metrics.get('completeness_pct'),
                'confidence_ratio': metrics.get('confidence_ratio'),
                'conflicts_detected': metrics.get('conflicts_detected'),
                'norm_success_pct': metrics.get('norm_success_pct', 0.0),
                'fact_count': metrics.get('fact_count')
            }
            validation_reports.append(report)

        report_df = pd.DataFrame(validation_reports)
        if not report_df.empty:
            log.info("Routing summary: %s", report_df['status'].value_counts().to_dict())
        return report_df