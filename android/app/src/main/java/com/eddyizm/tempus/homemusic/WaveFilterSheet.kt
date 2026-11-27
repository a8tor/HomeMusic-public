package com.eddyizm.tempus.homemusic

import android.os.Bundle
import android.view.View
import android.widget.TextView
import com.eddyizm.tempus.R
import com.eddyizm.tempus.ui.activity.MainActivity
import com.google.android.material.bottomsheet.BottomSheetDialogFragment
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup

class WaveFilterSheet : BottomSheetDialogFragment(R.layout.bottom_sheet_wave_filters) {
    private var draft = WaveFilters()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        draft = savedInstanceState?.getString(DRAFT)?.let { HubClient.gson.fromJson(it, WaveFilters::class.java) }
            ?: HubWave.savedFilters()
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        val summaryView = view.findViewById<TextView>(R.id.homemusic_wave_filter_draft)
        fun update(filters: WaveFilters) {
            draft = filters
            summaryView.text = summary(filters).ifEmpty { "Любой жанр · Любой язык · Любая знакомость" }
        }
        fun finish(filters: WaveFilters) {
            val host = activity as? MainActivity ?: return
            dismiss()
            HubWave.applyFilters(host, filters)
        }

        bindChips(view, R.id.homemusic_wave_filter_genres, genres, draft.genres) {
            update(draft.copy(genres = it))
        }
        bindChips(view, R.id.homemusic_wave_filter_lang, languages, listOfNotNull(draft.lang)) {
            update(draft.copy(lang = it.singleOrNull()))
        }
        bindChips(view, R.id.homemusic_wave_filter_familiarity, familiarity, listOfNotNull(draft.familiarity)) {
            update(draft.copy(familiarity = it.singleOrNull()))
        }
        bindChips(view, R.id.homemusic_wave_filter_energy, energy, listOfNotNull(draft.energy)) {
            update(draft.copy(energy = it.singleOrNull()))
        }
        update(draft)
        view.findViewById<View>(R.id.homemusic_wave_filter_apply).setOnClickListener { finish(draft) }
        view.findViewById<View>(R.id.homemusic_wave_filter_reset).setOnClickListener { finish(WaveFilters()) }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(DRAFT, HubClient.gson.toJson(draft))
    }

    private fun bindChips(
        view: View, groupId: Int, labels: Map<String, String>, checked: List<String>,
        changed: (List<String>) -> Unit,
    ) {
        val group = view.findViewById<ChipGroup>(groupId)
        val chips = labels.mapValues { (key, label) ->
            Chip(group.context).apply {
                id = View.generateViewId()
                text = label
                isCheckable = true
                isCheckedIconVisible = true
                isChecked = key in checked
                group.addView(this)
            }
        }
        group.setOnCheckedStateChangeListener { _, ids ->
            changed(chips.filterValues { it.id in ids }.keys.toList())
        }
    }

    companion object {
        const val TAG = "homemusic_wave_filter_sheet"
        private const val DRAFT = "draft"
        private val genres = linkedMapOf(
            "rock" to "Рок", "metal" to "Метал", "electronic" to "Электроника", "hiphop" to "Хип-хоп",
            "pop" to "Поп", "indie" to "Инди", "jazz" to "Джаз", "classical" to "Классика",
        )
        private val languages = mapOf("ru" to "Русская", "foreign" to "Иностранная")
        private val familiarity = mapOf("known" to "Знакомое", "new" to "Незнакомое")
        private val energy = mapOf("high" to "Энергичная", "low" to "Спокойная")

        fun summary(filters: WaveFilters): String = (
            genres.filterKeys { it in filters.genres.orEmpty() }.values +
                listOfNotNull(
                    filters.energy?.let(energy::get),
                    filters.lang?.let(languages::get),
                    filters.familiarity?.let(familiarity::get),
                )
            ).joinToString(" · ")
    }
}
