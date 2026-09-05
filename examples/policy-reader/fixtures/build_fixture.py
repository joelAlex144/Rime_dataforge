"""Build fixtures/policy.json — a SYNTHETIC homeowners insurance policy.

No real insurer, policyholder, or policy text is used. Structure and phrasing
follow the shape of a standard HO-3 form so the number handling (currency,
percentages, dates, section refs, policy numbers) is realistic.

Output schema (one record per clause; ordering by `index`):
  id              stable clause id, e.g. "sec-4b-ii"
  index           0-based reading order
  section         section number (int)
  section_title   title of the enclosing section
  subsection      letter or None
  text_display    what a sighted reader sees
  text_spoken     normalizer output (what is sent to Rime)
  sentences       [[start, end], ...] char offsets into text_display
  spoken_map      [[display_start, display_end, spoken], ...] from normalize_with_map

Run:  python examples/policy-reader/fixtures/build_fixture.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from delivery_layer.normalize import normalize_with_map  # noqa: E402

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"]
POLICY_NO = "HO-2026-048113"
EFFECTIVE = "January 1, 2026"
EXPIRY = "12/31/2026"

# Each section: (title, [ (subsection_letter or None, [clause texts]) ])
SECTIONS: list[tuple[str, list[tuple[str | None, list[str]]]]] = []


def S(title, *subs):
    SECTIONS.append((title, list(subs)))


S("Declarations",
  (None, [
      f"This policy, number {POLICY_NO}, is issued by Northlake Mutual Insurance Company to the named insured shown on the declarations page.",
      f"The policy period begins at 12:01 a.m. on {EFFECTIVE} and ends at 12:01 a.m. on {EXPIRY}, standard time at the residence premises.",
      "The residence premises is the one-family dwelling located at the address shown on the declarations page, together with its attached structures.",
      "The total annual premium for this policy is $1,842.00, payable in full or in 12 monthly installments of $153.50.",
      "A late payment fee of $25 applies to any installment received more than 10 days after its due date.",
      "The policy is subject to a deductible of $1,000 per occurrence unless a different deductible is shown for a specific peril.",
      "The windstorm and hail deductible is 2% of the Coverage A limit, which equals $7,000 at the limits shown in Section 2(a)(i).",
      "This policy replaces policy number HO-2025-031927, which expired on December 31, 2025.",
  ]))

S("Coverages and Limits",
  ("a", [
      "Coverage A, Dwelling, is provided with a limit of $350,000.",
      "Coverage B, Other Structures, is provided with a limit of 10% of Coverage A, which equals $35,000.",
      "Coverage C, Personal Property, is provided with a limit of 50% of Coverage A, which equals $175,000.",
      "Coverage D, Loss of Use, is provided with a limit of 20% of Coverage A, which equals $70,000.",
      "Coverage E, Personal Liability, is provided with a limit of $300,000 each occurrence.",
      "Coverage F, Medical Payments to Others, is provided with a limit of $5,000 each person.",
  ]),
  ("b", [
      "Limits shown for Coverages A through D are the most we will pay for all loss to that category of property in any one occurrence.",
      "The Coverage E limit is the most we will pay for all damages arising from any one occurrence, regardless of the number of insureds, claims made, or persons injured.",
      "Payments under Coverage F are not subject to the Coverage E limit and are made without regard to fault.",
      "Under Section 8(c)(ii), the Coverage A limit increases automatically by 4% at each renewal unless you decline the increase in writing.",
  ]))

S("Definitions",
  ("a", [
      "Throughout this policy, the words you and your refer to the named insured shown on the declarations page and the spouse if a resident of the same household.",
      "The words we, us, and our refer to Northlake Mutual Insurance Company.",
      "Bodily injury means bodily harm, sickness, or disease, including required care, loss of services, and death that results.",
      "Business means a trade, profession, or occupation engaged in on a full-time, part-time, or occasional basis, or any other activity engaged in for money or other compensation.",
      "An activity is not a business if it produced no more than $2,000 in total compensation during the 12 months before the policy period began.",
      "Insured means you and residents of your household who are your relatives, or other persons under the age of 21 in the care of any person named above.",
      "Insured location means the residence premises, the part of other premises used by you as a residence, and any premises used in connection with the residence premises.",
      "Occurrence means an accident, including continuous or repeated exposure to substantially the same general harmful conditions, which results in bodily injury or property damage during the policy period.",
  ]),
  ("b", [
      "Property damage means physical injury to, destruction of, or loss of use of tangible property.",
      "Residence employee means an employee of an insured whose duties are related to the maintenance or use of the residence premises, including household or domestic services.",
      "Residence premises means the one-family dwelling where you reside, shown as the residence premises in the declarations, including the grounds and structures at that location.",
      "Actual cash value means the cost to replace damaged property with new property of like kind and quality, less depreciation, as described in Section 9(b)(i).",
      "Replacement cost means the cost, at the time of loss, to repair or replace the damaged property with material of like kind and quality without deduction for depreciation.",
      "Fungi means any type or form of fungus, including mold or mildew, and any mycotoxins, spores, scents, or by-products produced or released by fungi.",
      "Motor vehicle means a self-propelled land or amphibious vehicle, or any trailer or semitrailer designed to be towed by one.",
      "Vacant means a dwelling that has been without occupants and substantially empty of furnishings for more than 60 consecutive days.",
  ]))

S("Perils Insured Against",
  ("a", [
      "Under Coverage A and Coverage B we insure against direct physical loss to property, except as excluded in Section 5 and in this section.",
      "We do not insure for loss involving collapse, other than as provided in Section 6(d).",
      "We do not insure for loss caused by freezing of a plumbing, heating, air conditioning, or automatic fire protective sprinkler system, or of a household appliance, while the dwelling is vacant, unoccupied, or under construction, unless you used reasonable care to maintain heat in the building or shut off the water supply and drained the system.",
      "We do not insure for loss caused by freezing, thawing, pressure, or weight of water or ice to a fence, pavement, patio, swimming pool, foundation, retaining wall, bulkhead, pier, wharf, or dock.",
      "We do not insure for loss caused by theft in or to a dwelling under construction, or of materials and supplies for use in the construction, until the dwelling is finished and occupied.",
      "We do not insure for loss caused by vandalism or malicious mischief if the dwelling has been vacant for more than 60 consecutive days immediately before the loss.",
  ]),
  ("b", [
      "We do not insure for loss caused by wear and tear, marring, deterioration, or inherent vice.",
      "We do not insure for loss caused by smog, rust, corrosion, mold, wet or dry rot, or by smoke from agricultural smudging or industrial operations.",
      "We do not insure for loss caused by discharge, dispersal, seepage, migration, release, or escape of pollutants, unless the discharge is itself caused by a peril insured against under Coverage C.",
      "We do not insure for loss caused by settling, shrinking, bulging, or expansion, including resultant cracking, of pavements, patios, foundations, walls, floors, roofs, or ceilings.",
      "We do not insure for loss caused by birds, vermin, rodents, or insects.",
      "We do not insure for loss caused by animals owned or kept by an insured.",
      "If any of the causes listed in Section 4(b)(i) through 4(b)(vi) results in a sudden and accidental discharge of water or steam from a plumbing, heating, or air conditioning system, we do cover the resulting damage, and we cover the cost of tearing out and replacing any part of the building necessary to repair the system, but not the system itself.",
      "Water damage resulting from a discharge described in Section 4(b)(vii) is subject to the sublimit of $10,000 stated in Section 7(a)(iii).",
  ]),
  ("c", [
      "Under Coverage C we insure for direct physical loss caused by fire or lightning.",
      "Under Coverage C we insure for direct physical loss caused by windstorm or hail, but we do not insure loss to the interior of a building caused by rain, snow, sleet, sand, or dust unless the direct force of wind or hail first damages the building, causing an opening in a roof or wall.",
      "Under Coverage C we insure for direct physical loss caused by explosion, riot or civil commotion, aircraft, and vehicles.",
      "Under Coverage C we insure for direct physical loss caused by smoke, meaning sudden and accidental damage from smoke, but not from agricultural smudging or industrial operations.",
      "Under Coverage C we insure for direct physical loss caused by vandalism or malicious mischief, and by theft, including attempted theft and loss of property from a known place when it is likely that the property has been stolen.",
      "Theft coverage does not include theft committed by an insured, theft from a dwelling under construction, or theft from any part of the residence premises rented by an insured to someone other than an insured.",
      "Under Coverage C we insure for direct physical loss caused by falling objects, but not to property contained in a building unless the roof or an outside wall is first damaged by the falling object.",
      "Under Coverage C we insure for direct physical loss caused by weight of ice, snow, or sleet that causes damage to property contained in a building.",
      "Under Coverage C we insure for direct physical loss caused by sudden and accidental tearing apart, cracking, burning, or bulging of a steam, hot water, air conditioning, or automatic fire protective sprinkler system.",
      "Under Coverage C we insure for direct physical loss caused by sudden and accidental damage from artificially generated electrical current, but not to tubes, transistors, electronic components, or circuitry that are part of appliances, fixtures, computers, or home entertainment units.",
      "Under Coverage C we insure for direct physical loss caused by volcanic eruption, other than loss caused by earthquake, land shock waves, or tremors.",
  ]))

S("General Exclusions",
  ("a", [
      "We do not insure for loss caused directly or indirectly by any of the following, and such loss is excluded regardless of any other cause or event contributing concurrently or in any sequence to the loss.",
      "Ordinance or law, meaning enforcement of any ordinance or law regulating the construction, repair, or demolition of a building, except as provided in Section 6(a).",
      "Earth movement, meaning earthquake, land shock waves or tremors before, during, or after a volcanic eruption, landslide, mine subsidence, mudflow, or earth sinking, rising, or shifting.",
      "Direct loss by fire, explosion, or theft resulting from earth movement is covered.",
      "Water damage, meaning flood, surface water, waves, tidal water, overflow of a body of water, or spray from any of these, whether or not driven by wind.",
      "Water damage also means water that backs up through sewers or drains or that overflows from a sump, and water below the surface of the ground that exerts pressure on or seeps through a building, sidewalk, driveway, foundation, swimming pool, or other structure.",
      "Direct loss by fire, explosion, or theft resulting from water damage is covered.",
      "Coverage for flood may be purchased separately as an endorsement and is not provided by this policy.",
  ]),
  ("b", [
      "Power failure, meaning the failure of power or other utility service if the failure takes place off the residence premises; if a peril insured against ensues on the residence premises, we cover loss caused by that ensuing peril.",
      "Neglect, meaning neglect of the insured to use all reasonable means to save and preserve property at and after the time of a loss.",
      "War, including undeclared war, civil war, insurrection, rebellion, revolution, warlike act by a military force or military personnel, destruction or seizure or use for a military purpose, and any consequence of any of these.",
      "Nuclear hazard, to the extent set forth in Section 10(b)(iv).",
      "Intentional loss, meaning any loss arising out of any act committed by or at the direction of an insured with the intent to cause a loss.",
      "Governmental action, meaning the destruction, confiscation, or seizure of property by order of any governmental or public authority, except when ordered to prevent the spread of fire.",
  ]),
  ("c", [
      "We do not insure for loss to property described in Coverages A and B caused by weather conditions, but this exclusion applies only if weather conditions contribute in any way with a cause or event excluded in Section 5(a) to produce the loss.",
      "We do not insure for loss caused by acts or decisions, including the failure to act or decide, of any person, group, organization, or governmental body.",
      "We do not insure for loss caused by faulty, inadequate, or defective planning, zoning, development, surveying, siting, design, specifications, workmanship, repair, construction, renovation, remodeling, grading, compaction, materials, or maintenance.",
      "If an excluded cause of loss listed in Section 5(c)(i) through 5(c)(iii) results in a loss that is otherwise covered, we will pay for that ensuing loss.",
  ]))

S("Additional Coverages",
  ("a", [
      "Ordinance or Law: you may use up to 10% of the Coverage A limit, which equals $35,000, for the increased costs you incur due to the enforcement of any ordinance or law that requires or regulates the construction, demolition, remodeling, renovation, or repair of the dwelling.",
      "This coverage does not increase the Coverage A limit and does not apply to costs of testing for, monitoring, cleaning up, or removing pollutants.",
  ]),
  ("b", [
      "Debris Removal: we will pay your reasonable expense for the removal of debris of covered property if a peril insured against causes the loss.",
      "If the amount payable for the actual damage to the property plus the debris removal expense is more than the limit of liability for the damaged property, an additional 5% of that limit is available for debris removal.",
      "We will also pay up to $1,000 for the removal of trees felled by windstorm, hail, or weight of ice, snow, or sleet, provided the tree damages a covered structure, with no more than $500 paid for any one tree.",
  ]),
  ("c", [
      "Reasonable Repairs: in the event that covered property is damaged by a peril insured against, we will pay the reasonable cost incurred by you for the necessary measures taken solely to protect covered property from further damage.",
      "Trees, Shrubs and Other Plants: we cover trees, shrubs, plants, or lawns on the residence premises for loss caused by fire, lightning, explosion, riot, aircraft, vehicles not owned by a resident, vandalism, or theft.",
      "The limit for trees, shrubs and other plants is 5% of the Coverage A limit, which equals $17,500, and no more than $750 of this limit will be paid for any one tree, shrub, or plant.",
      "Fire Department Service Charge: we will pay up to $500 for your liability assumed by contract for fire department charges incurred when the fire department is called to save or protect covered property from a peril insured against, with no deductible applying.",
  ]),
  ("d", [
      "Collapse: we insure for direct physical loss to covered property involving collapse of a building or any part of a building caused only by hidden decay, hidden insect or vermin damage, weight of contents, weight of rain that collects on a roof, or use of defective material or methods in construction if the collapse occurs during the course of construction.",
      "Collapse does not include settling, cracking, shrinking, bulging, or expansion.",
      "Loss to an awning, fence, patio, pavement, swimming pool, underground pipe, flue, drain, cesspool, septic tank, foundation, retaining wall, bulkhead, pier, wharf, or dock is not included unless the loss is a direct result of the collapse of a building.",
      "Glass or Safety Glazing Material: we cover the breakage of glass or safety glazing material which is part of a covered building, storm door, or storm window, and damage to covered property by glass or safety glazing material that is part of a building.",
      "Landlord's Furnishings: we will pay up to $2,500 for your appliances, carpeting, and other household furnishings in an apartment on the residence premises regularly rented or held for rental to others by an insured, for loss caused by a peril insured against under Coverage C.",
      "Credit Card, Fund Transfer Card, Forgery and Counterfeit Money: we will pay up to $500 for the legal obligation of an insured to pay because of the theft or unauthorized use of credit cards or fund transfer cards issued to or registered in an insured's name.",
      "Loss Assessment: we will pay up to $1,000 for your share of loss assessment charged during the policy period against you by a corporation or association of property owners, when the assessment is made as a result of direct loss to property owned by all members collectively.",
  ]))

S("Special Limits of Liability",
  ("a", [
      "The special limits in this section do not increase the Coverage C limit; they are the total limit for each numbered category.",
      "$200 on money, bank notes, bullion, gold other than goldware, silver other than silverware, platinum, coins, and medals.",
      "$10,000 on water damage resulting from a discharge described in Section 4(b)(vii), inclusive of tear-out costs.",
      "$1,500 on securities, accounts, deeds, evidences of debt, letters of credit, notes other than bank notes, manuscripts, passports, tickets, and stamps.",
      "$1,500 on watercraft, including their trailers, furnishings, equipment, and outboard motors.",
      "$1,500 on trailers not used with watercraft.",
      "$1,500 for loss by theft of jewelry, watches, furs, precious and semiprecious stones.",
      "$2,500 for loss by theft of firearms.",
      "$2,500 for loss by theft of silverware, silver-plated ware, goldware, gold-plated ware, and pewterware.",
      "$2,500 on property, on the residence premises, used at any time or in any manner for any business purpose.",
      "$500 on property, away from the residence premises, used at any time or in any manner for any business purpose.",
      "$1,500 for loss to electronic apparatus, while in or upon a motor vehicle or other motorized land conveyance, if the apparatus is equipped to be operated by power from the electrical system of the vehicle.",
  ]),
  ("b", [
      "Property not covered under Coverage C includes articles separately described and specifically insured in this or any other insurance.",
      "Property not covered includes animals, birds, or fish.",
      "Property not covered includes motor vehicles or all other motorized land conveyances, including their equipment and accessories, except vehicles not subject to motor vehicle registration that are used to service an insured's residence or designed for assisting the handicapped.",
      "Property not covered includes aircraft and parts, other than model or hobby aircraft not used or designed to carry people or cargo.",
      "Property not covered includes property of roomers, boarders, and other tenants, except property of roomers and boarders related to an insured.",
      "Property not covered includes property in an apartment regularly rented or held for rental to others by an insured, except as provided in Section 6(d)(v).",
      "Property not covered includes property rented or held for rental to others off the residence premises.",
      "Property not covered includes business data, including such data stored in books of account, drawings, or other paper records, or in computers and related equipment, but we do cover the cost of blank recording or storage media and of prerecorded computer programs available on the retail market.",
      "Property not covered includes credit cards or fund transfer cards except as provided in Section 6(d)(vi).",
  ]))

S("Premium, Renewal and Cancellation",
  ("a", [
      "The premium shown on the declarations page is based on information you provided at the time of application; if that information is inaccurate, we may adjust the premium retroactively to the effective date.",
      "If the adjusted premium exceeds the premium paid by more than 25%, we will notify you in writing at least 30 days before the adjustment takes effect.",
      "A returned payment fee of $30 applies to any payment that is not honored by your financial institution.",
  ]),
  ("b", [
      "You may cancel this policy at any time by returning it to us or by notifying us in writing of the date cancellation is to take effect.",
      "If you cancel, we will refund the unearned premium on a pro rata basis within 30 days of the cancellation date.",
      "We may cancel this policy for nonpayment of premium by notifying you in writing at least 10 days before the date cancellation takes effect.",
      "When this policy has been in effect for less than 60 days and is not a renewal with us, we may cancel for any reason by notifying you at least 30 days before the date cancellation takes effect.",
      "When this policy has been in effect for 60 days or more, we may cancel only for material misrepresentation, substantial change in risk, or nonpayment, by notifying you at least 45 days before the date cancellation takes effect.",
  ]),
  ("c", [
      "We will not fail to renew this policy without notifying you in writing at least 60 days before the expiration date shown on the declarations page.",
      "At each renewal the Coverage A limit will be increased by 4% to reflect changes in construction costs, and the premium will be adjusted accordingly, unless you notify us in writing that you decline the increase.",
      "Under Section 2(b)(iv), the increased limit applies only to losses occurring after the renewal effective date.",
      "If we offer renewal at a premium more than 15% higher than the expiring premium, other than as a result of a change in coverage or a change in the property, we will state the reason for the increase in the renewal notice.",
  ]))

S("Conditions Applicable to Property Coverages",
  ("a", [
      "Insurable Interest: even if more than one person has an insurable interest in the property covered, we will not be liable in any one loss to an insured for more than the amount of the insured's interest at the time of loss, or for more than the applicable limit of liability.",
      "Your Duties After Loss: in case of a loss to covered property, you must give prompt notice to us or our agent, and notify the police in case of loss by theft.",
      "You must notify the credit card or fund transfer card company in case of loss under Section 6(d)(vi).",
      "You must protect the property from further damage, make reasonable and necessary repairs to protect the property, and keep an accurate record of repair expenses.",
      "You must prepare an inventory of damaged personal property showing the quantity, description, actual cash value, and amount of loss, attaching all bills, receipts, and related documents that justify the figures in the inventory.",
      "You must, as often as we reasonably require, show the damaged property, provide us with records and documents we request and permit us to make copies, and submit to examination under oath while not in the presence of any other insured.",
      "You must send to us, within 60 days after our request, your signed, sworn proof of loss which sets forth, to the best of your knowledge and belief, the time and cause of loss, the interest of the insured and all others in the property involved, and all liens on the property.",
  ]),
  ("b", [
      "Loss Settlement: covered property losses are settled as follows.",
      "Personal property, awnings, carpeting, household appliances, outdoor antennas, and outdoor equipment, whether or not attached to buildings, and structures that are not buildings, are settled at actual cash value at the time of loss but not more than the amount required to repair or replace.",
      "Buildings under Coverage A or B are settled at replacement cost without deduction for depreciation, subject to the following conditions.",
      "If at the time of loss the amount of insurance in this policy on the damaged building is 80% or more of the full replacement cost of the building immediately before the loss, we will pay the cost to repair or replace, after application of the deductible and without deduction for depreciation, but not more than the least of the limit of liability, the replacement cost of that part of the building damaged with material of like kind and quality, or the amount actually and necessarily spent to repair or replace.",
      "If at the time of loss the amount of insurance on the damaged building is less than 80% of the full replacement cost, we will pay the greater of the actual cash value of that part of the building damaged, or that proportion of the cost to repair or replace which the amount of insurance bears to 80% of the replacement cost.",
      "We will pay no more than the actual cash value of the damage until actual repair or replacement is complete, unless the cost to repair or replace is both less than 5% of the amount of insurance on the building and less than $2,500.",
      "You may disregard the replacement cost loss settlement provisions and make claim under this policy for loss to buildings on an actual cash value basis, and you may then make claim within 180 days after loss for any additional liability on a replacement cost basis.",
  ]),
  ("c", [
      "Loss to a Pair or Set: in case of loss to a pair or set we may elect to repair or replace any part to restore the pair or set to its value before the loss, or pay the difference between actual cash value of the property before and after the loss.",
      "Glass Replacement: loss for damage to glass caused by a peril insured against will be settled on the basis of replacement with safety glazing materials when required by ordinance or law.",
      "Appraisal: if you and we fail to agree on the amount of loss, either may demand an appraisal of the loss, and each party will choose a competent appraiser within 20 days after receiving a written request from the other.",
      "The two appraisers will choose an umpire; if they cannot agree upon an umpire within 15 days, you or we may request that the choice be made by a judge of a court of record in the state where the residence premises is located.",
      "Each party will pay its own appraiser and bear the other expenses of the appraisal and umpire equally.",
      "Other Insurance: if a loss covered by this policy is also covered by other insurance, we will pay only the proportion of the loss that the limit of liability that applies under this policy bears to the total amount of insurance covering the loss.",
      "Suit Against Us: no action can be brought against us unless there has been full compliance with all of the terms of this policy and the action is started within 2 years after the date of loss.",
      "Our Option: if we give you written notice within 30 days after we receive your signed, sworn proof of loss, we may repair or replace any part of the damaged property with like property.",
      "Loss Payment: we will adjust all losses with you and will pay you unless some other person is named in the policy or is legally entitled to receive payment; loss will be payable 60 days after we receive your proof of loss and reach agreement with you, or there is an entry of a final judgment, or there is a filing of an appraisal award with us.",
      "Abandonment of Property: we need not accept any property abandoned by an insured.",
      "Mortgage Clause: if a mortgagee is named in this policy, any loss payable under Coverage A or B will be paid to the mortgagee and you, as interests appear, and we will give the mortgagee at least 10 days notice before cancellation.",
      "No Benefit to Bailee: we will not recognize any assignment or grant any coverage that benefits a person or organization holding, storing, or moving property for a fee regardless of any other provision of this policy.",
      "Recovered Property: if you or we recover any property for which we have made payment under this policy, you or we will notify the other of the recovery, and at your option the property will be returned to or retained by you or it will become our property.",
      "Volcanic Eruption Period: one or more volcanic eruptions that occur within a 72-hour period will be considered as one volcanic eruption.",
  ]))

S("Liability Coverages",
  ("a", [
      "Coverage E, Personal Liability: if a claim is made or a suit is brought against an insured for damages because of bodily injury or property damage caused by an occurrence to which this coverage applies, we will pay up to our limit of liability for the damages for which the insured is legally liable.",
      "Damages include prejudgment interest awarded against the insured.",
      "We will provide a defense at our expense by counsel of our choice, even if the suit is groundless, false, or fraudulent, and we may investigate and settle any claim or suit that we decide is appropriate.",
      "Our duty to settle or defend ends when the amount we pay for damages resulting from the occurrence equals our limit of liability, which is $300,000 as stated in Section 2(a)(v).",
  ]),
  ("b", [
      "Coverage F, Medical Payments to Others: we will pay the necessary medical expenses that are incurred or medically ascertained within 3 years from the date of an accident causing bodily injury.",
      "Medical expenses means reasonable charges for medical, surgical, x-ray, dental, ambulance, hospital, professional nursing, prosthetic devices, and funeral services.",
      "This coverage does not apply to you or regular residents of your household, except residence employees.",
      "Nuclear Hazard: this coverage does not apply to bodily injury or property damage arising out of nuclear reaction, radiation, or radioactive contamination, all whether controlled or uncontrolled or however caused, or any consequence of any of these.",
  ]),
  ("c", [
      "Coverages E and F do not apply to bodily injury or property damage which is expected or intended by an insured.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of or in connection with a business conducted from an insured location or engaged in by an insured, whether or not the business is owned or operated by an insured.",
      "This exclusion does not apply to activities which are usual to non-business pursuits, or to the rental or holding for rental of an insured location on an occasional basis for use only as a residence.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of the rendering of or failure to render professional services.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of a premises owned by an insured, rented to an insured, or rented to others by an insured, that is not an insured location.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of the ownership, maintenance, occupancy, operation, use, loading, or unloading of motor vehicles or all other motorized land conveyances owned or operated by or rented or loaned to an insured.",
      "This exclusion does not apply to a motorized land conveyance designed for recreational use off public roads, not subject to motor vehicle registration, and not owned by an insured, or owned by an insured and on an insured location.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of the ownership, maintenance, use, loading, or unloading of watercraft that is 26 feet or more in overall length, or that has inboard or inboard-outdrive motor power of more than 50 horsepower, and that is owned by or rented to an insured.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of the transmission of a communicable disease by an insured.",
      "Coverages E and F do not apply to bodily injury or property damage arising out of the use, sale, manufacture, delivery, transfer, or possession by any person of a controlled substance, other than the legitimate use of prescription drugs by a person following the orders of a licensed physician.",
      "Coverage E does not apply to liability for your share of any loss assessment charged against all members of an association, corporation, or community of property owners, except as provided in Section 6(d)(vii).",
      "Coverage E does not apply to liability under any contract or agreement, except written contracts that directly relate to the ownership, maintenance, or use of an insured location, or where the liability of others is assumed by you prior to an occurrence.",
      "Coverage E does not apply to property damage to property owned by an insured, or to property rented to, occupied or used by, or in the care of an insured, except for property damage caused by fire, smoke, or explosion.",
      "Coverage E does not apply to bodily injury to any person eligible to receive any benefits voluntarily provided or required to be provided by an insured under any workers' compensation, non-occupational disability, or occupational disease law.",
  ]))

S("Additional Liability Coverages",
  ("a", [
      "Claim Expenses: we pay expenses we incur and costs taxed against an insured in any suit we defend.",
      "We pay premiums on bonds required in a suit we defend, but not for bond amounts more than the limit of liability for Coverage E, and we need not apply for or furnish any bond.",
      "We pay reasonable expenses incurred by an insured at our request, including actual loss of earnings, but not loss of other income, up to $250 per day, for assisting us in the investigation or defense of a claim or suit.",
      "We pay interest on the entire judgment which accrues after entry of the judgment and before we pay or tender, or deposit in court, that part of the judgment which does not exceed the limit of liability that applies.",
  ]),
  ("b", [
      "First Aid Expenses: we will pay expenses for first aid to others incurred by an insured for bodily injury covered under this policy, but we will not pay for first aid to you or any other insured.",
      "Damage to Property of Others: we will pay, at replacement cost, up to $1,000 per occurrence for property damage to property of others caused by an insured.",
      "We will not pay for property damage to the extent of any amount recoverable under Section 4 of this policy, or caused intentionally by an insured who is 13 years of age or older.",
      "Loss Assessment: we will pay up to $1,000 for your share of loss assessment charged during the policy period against you by a corporation or association of property owners, when the assessment is made as a result of bodily injury or property damage not excluded under Section 10(c).",
      "We do not cover loss assessments charged against you or a corporation or association of property owners by any governmental body.",
      "Regardless of the number of assessments, the limit of $1,000 is the most we will pay for loss arising out of one accident or one covered act of a director, officer, or trustee.",
  ]))

S("Conditions Applicable to Liability Coverages",
  ("a", [
      "Limit of Liability: our total liability under Coverage E for all damages resulting from any one occurrence will not be more than the limit of liability for Coverage E as shown in the declarations, and this limit is the same regardless of the number of insureds, claims made, or persons injured.",
      "Our total liability under Coverage F for all medical expense payable for bodily injury to one person as the result of one accident will not be more than the limit of liability for Coverage F as shown in the declarations.",
      "Severability of Insurance: this insurance applies separately to each insured, but this condition will not increase our limit of liability for any one occurrence.",
      "Duties After Occurrence: in case of an accident or occurrence, the insured must give written notice to us or our agent as soon as is practical, setting forth the identity of the policy and the named insured, reasonably available information on the time, place, and circumstances of the accident or occurrence, and names and addresses of any claimants and witnesses.",
      "The insured must promptly forward to us every notice, demand, summons, or other process relating to the accident or occurrence.",
      "The insured must, at our request, help us to make settlement, to enforce any right of contribution or indemnity against any person or organization who may be liable to an insured, and with the conduct of suits and attend hearings and trials.",
      "The insured will not, except at the insured's own cost, voluntarily make payment, assume obligation, or incur expense other than for first aid to others at the time of the bodily injury.",
  ]),
  ("b", [
      "Duties of an Injured Person under Coverage F: the injured person or someone acting for the injured person will give us written proof of claim, under oath if required, as soon as is practical, and authorize us to obtain copies of medical reports and records.",
      "The injured person will submit to a physical examination by a doctor of our choice when and as often as we reasonably require.",
      "Payment of Claim under Coverage F: payment under this coverage is not an admission of liability by an insured or us.",
      "Suit Against Us: no action can be brought against us unless there has been full compliance with all of the terms under this section, and no one will have the right to join us as a party to any action against an insured.",
      "Bankruptcy of an Insured: bankruptcy or insolvency of an insured will not relieve us of our obligations under this policy.",
      "Other Insurance under Coverage E: this insurance is excess over other valid and collectible insurance except insurance written specifically to cover as excess over the limits of liability that apply in this policy.",
  ]))

S("General Conditions",
  ("a", [
      "Policy Period: this policy applies only to loss under Coverages A through D and bodily injury or property damage under Coverages E and F which occurs during the policy period stated in Section 1(a)(ii).",
      "Concealment or Fraud: the entire policy will be void if, whether before or after a loss, an insured has intentionally concealed or misrepresented any material fact or circumstance, engaged in fraudulent conduct, or made false statements relating to this insurance.",
      "Liberalization Clause: if we make a change which broadens coverage under this edition of our policy without additional premium charge, that change will automatically apply to your insurance as of the date we implement the change in your state, provided that this implementation date falls within 60 days prior to or during the policy period.",
      "Waiver or Change of Policy Provisions: a waiver or change of a provision of this policy must be in writing by us to be valid, and our request for an appraisal or examination will not waive any of our rights.",
      "Assignment: assignment of this policy will not be valid unless we give our written consent.",
      "Subrogation: an insured may waive in writing before a loss all rights of recovery against any person; if not waived, we may require an assignment of rights of recovery for a loss to the extent that payment is made by us.",
      "Death: if any person named in the declarations or the spouse, if a resident of the same household, dies, we insure the legal representative of the deceased but only with respect to the premises and property of the deceased covered under the policy at the time of death.",
  ]),
  ("b", [
      "Nuclear Hazard Clause: nuclear hazard means any nuclear reaction, radiation, or radioactive contamination, all whether controlled or uncontrolled or however caused, or any consequence of any of these.",
      "Loss caused by the nuclear hazard will not be considered loss caused by fire, explosion, or smoke, whether these perils are specifically named in or otherwise included within the perils insured against.",
      "This policy does not apply to loss caused directly or indirectly by nuclear hazard, except that direct loss by fire resulting from the nuclear hazard is covered.",
      "Recovered Property under Liability: if we pay a claim and later recover from a third party, the recovery is applied first to our expenses of recovery, then to our payment, and any remainder is paid to you.",
      "Notices: any notice we are required to give under this policy will be mailed or delivered to your last known address, and proof of mailing is sufficient proof of notice.",
      "Conformity to Statutes: any provision of this policy that conflicts with the statutes of the state in which the residence premises is located is amended to conform to those statutes.",
      "Questions about coverage should be directed to Northlake Mutual at the telephone number shown on the declarations page, or in writing to the address shown there, quoting policy number HO-2026-048113.",
  ]))


_SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"$(])|(?<=;)\s+")


def sentence_spans(text: str) -> list[list[int]]:
    """Char spans of sentences. Splits on . ! ? followed by whitespace+capital, and on any semicolon.
    Decimal numbers ("4.2.1", "$1,842.00") never match because no whitespace follows the dot."""
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        spans.append([start, m.start()])
        start = m.end()
    spans.append([start, len(text)])
    return spans


def build() -> list[dict]:
    clauses: list[dict] = []
    idx = 0
    for sec_no, (title, subs) in enumerate(SECTIONS, start=1):
        for letter, texts in subs:
            for k, text in enumerate(texts):
                roman = ROMAN[k]
                cid = f"sec-{sec_no}{letter or ''}-{roman}"
                spoken, segs = normalize_with_map(text)
                clauses.append({
                    "id": cid,
                    "index": idx,
                    "section": sec_no,
                    "section_title": title,
                    "subsection": letter,
                    "item": roman,
                    "text_display": text,
                    "text_spoken": spoken,
                    "sentences": sentence_spans(text),
                    "spoken_map": [[s.display_start, s.display_end, s.spoken] for s in segs],
                })
                idx += 1
    return clauses


def main() -> None:
    clauses = build()
    ids = [c["id"] for c in clauses]
    assert len(ids) == len(set(ids)), "duplicate clause ids"
    assert 150 <= len(clauses) <= 250, f"clause count {len(clauses)} outside 150–250"
    out = Path(__file__).with_name("policy.json")
    doc = {
        "title": "Northlake Mutual Homeowners Policy (SYNTHETIC FIXTURE)",
        "synthetic": True,
        "policy_number": POLICY_NO,
        "note": "Entirely synthetic. No real insurer, insured, or policy text. For testing a voice document reader.",
        "clause_count": len(clauses),
        "clauses": clauses,
    }
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"wrote {out} with {len(clauses)} clauses across {len(SECTIONS)} sections")


if __name__ == "__main__":
    main()
