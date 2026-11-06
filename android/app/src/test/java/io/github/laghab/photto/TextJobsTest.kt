package io.github.laghab.photto

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class TextJobsTest {
    @Test fun samplesByDurationBucket() {
        assertEquals(listOf(1, 1, 2, 2, 5), listOf(8.0, 14.99, 15.0, 59.99, 60.0).map { TextJobs.sampleInterval(it) })
    }

    @Test fun queryMatchesBelowThresholdButExcludesNonmatches() {
        assertEquals("query", TextJobs.matchReason("Invoice", 2, "voice"))
        assertNull(TextJobs.matchReason("Invoice document reference", 2, "receipt"))
        assertEquals("threshold", TextJobs.matchReason("Invoice document", 2, ""))
        assertEquals(3, TextJobs.meaningfulWordCount("OK cat 4812 naïve dog"))
    }
}
